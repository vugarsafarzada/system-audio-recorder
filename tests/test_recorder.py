from __future__ import annotations

import tempfile
import time
import unittest
import wave
from pathlib import Path

from system_audio_recorder.native import NativeCaptureError, NativeCaptureResult
from system_audio_recorder.recorder import (
    EmptyRecordingError,
    RecordingConfig,
    RecordingError,
    SystemAudioRecorder,
)


class FakeCaptureSession:
    def __init__(
        self,
        output_path: Path,
        sample_rate: int,
        *,
        frames: int = 4_800,
        start_error: str | None = None,
        stop_error: str | None = None,
        corrupt: bool = False,
    ) -> None:
        self.output_path = output_path
        self.sample_rate = sample_rate
        self.frames = frames
        self.start_error = start_error
        self.stop_error = stop_error
        self.corrupt = corrupt
        self.started = False
        self.aborted = False
        self.started_at: float | None = None
        self.failure: str | None = None

    @property
    def elapsed_seconds(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.monotonic() - self.started_at

    def start(self) -> None:
        if self.start_error:
            raise NativeCaptureError(self.start_error)
        self.started = True
        self.started_at = time.monotonic()

    def stop(self) -> NativeCaptureResult:
        if self.corrupt:
            self.output_path.write_bytes(b"not-a-wave")
        elif self.frames > 0:
            write_test_wav(self.output_path, self.sample_rate, self.frames)
        if self.stop_error:
            raise NativeCaptureError(self.stop_error)
        return NativeCaptureResult(
            frames=self.frames,
            duration_seconds=self.frames / self.sample_rate,
            warnings=(),
        )

    def abort(self) -> None:
        self.aborted = True


def write_test_wav(path: Path, sample_rate: int, frames: int) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(2)
        recording.setsampwidth(2)
        recording.setframerate(sample_rate)
        recording.writeframes(b"\x01\x00\x02\x00" * frames)


class SessionFactory:
    def __init__(self, **options):
        self.options = options
        self.sessions: list[FakeCaptureSession] = []

    def __call__(self, output_path: Path, sample_rate: int) -> FakeCaptureSession:
        session = FakeCaptureSession(output_path, sample_rate, **self.options)
        self.sessions.append(session)
        return session


class RecorderLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_config(self, sample_rate: int = 48_000) -> RecordingConfig:
        return RecordingConfig(
            sample_rate=sample_rate,
            output_format="wav",
            output_directory=self.output_dir,
        )

    def test_wav_recording_lifecycle(self) -> None:
        factory = SessionFactory(frames=4_800)
        recorder = SystemAudioRecorder(self.make_config(), capture_factory=factory)

        self.assertEqual(factory.sessions, [])
        recorder.start()
        result = recorder.stop()

        self.assertTrue(factory.sessions[0].started)
        self.assertEqual(result.actual_format, "wav")
        self.assertAlmostEqual(result.duration_seconds, 0.1)
        with wave.open(str(result.path), "rb") as recording:
            self.assertEqual(recording.getnchannels(), 2)
            self.assertEqual(recording.getsampwidth(), 2)
            self.assertEqual(recording.getframerate(), 48_000)
            self.assertEqual(recording.getnframes(), 4_800)

    def test_repeated_sessions_do_not_overwrite(self) -> None:
        first = SystemAudioRecorder(
            self.make_config(), capture_factory=SessionFactory()
        )
        second = SystemAudioRecorder(
            self.make_config(), capture_factory=SessionFactory()
        )
        first.start()
        first_result = first.stop()
        first_bytes = first_result.path.read_bytes()
        second.start()
        second_result = second.stop()

        self.assertNotEqual(first_result.path, second_result.path)
        self.assertEqual(first_result.path.read_bytes(), first_bytes)

    def test_empty_recording_is_removed(self) -> None:
        recorder = SystemAudioRecorder(
            self.make_config(), capture_factory=SessionFactory(frames=0)
        )
        recorder.start()
        with self.assertRaises(EmptyRecordingError):
            recorder.stop()
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_permission_denial_is_reported_and_cleaned_up(self) -> None:
        factory = SessionFactory(start_error="permission denied")
        recorder = SystemAudioRecorder(self.make_config(), capture_factory=factory)

        with self.assertRaisesRegex(RecordingError, "permission denied"):
            recorder.start()
        self.assertTrue(factory.sessions[0].aborted)
        self.assertEqual(list(self.output_dir.iterdir()), [])

    def test_interrupted_valid_recording_is_preserved(self) -> None:
        factory = SessionFactory(stop_error="capture interrupted")
        recorder = SystemAudioRecorder(self.make_config(), capture_factory=factory)
        recorder.start()
        result = recorder.stop()

        self.assertTrue(result.path.exists())
        self.assertIn("capture interrupted", result.warnings[0])

    def test_corrupt_recording_is_not_published(self) -> None:
        factory = SessionFactory(corrupt=True)
        recorder = SystemAudioRecorder(self.make_config(), capture_factory=factory)
        recorder.start()

        with self.assertRaisesRegex(RecordingError, "invalid WAV"):
            recorder.stop()
        self.assertEqual(list(self.output_dir.glob("system-audio_*.wav")), [])

    def test_both_supported_sample_rates(self) -> None:
        for sample_rate in (44_100, 48_000):
            with self.subTest(sample_rate=sample_rate):
                recorder = SystemAudioRecorder(
                    self.make_config(sample_rate), capture_factory=SessionFactory()
                )
                recorder.start()
                result = recorder.stop()
                with wave.open(str(result.path), "rb") as recording:
                    self.assertEqual(recording.getframerate(), sample_rate)

    def test_mp3_is_explicitly_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "no native MP3 encoder"):
            RecordingConfig(output_format="mp3", output_directory=self.output_dir)

    def test_invalid_sample_rate_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "44100 or 48000"):
            RecordingConfig(sample_rate=96_000, output_directory=self.output_dir)


if __name__ == "__main__":
    unittest.main()

