from __future__ import annotations

import os
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from system_audio_recorder.library import (
    FolderOpenError,
    PlaybackError,
    add_recording_name,
    list_recordings,
    open_recordings_folder,
    play_recording,
)


def write_test_wav(path: Path, frames: int = 4_800) -> None:
    with wave.open(str(path), "wb") as recording:
        recording.setnchannels(2)
        recording.setsampwidth(2)
        recording.setframerate(48_000)
        recording.writeframes(b"\x01\x00\x02\x00" * frames)


class RecordingLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_name_is_safe_and_preserves_timestamp(self) -> None:
        original = self.directory / "system-audio_2026-10-13_12-00-00.wav"
        write_test_wav(original)

        renamed = add_recording_name(original, "My / favorite: song")

        self.assertEqual(
            renamed.name,
            "system-audio_2026-10-13_12-00-00_My-favorite-song.wav",
        )
        self.assertTrue(renamed.exists())
        self.assertFalse(original.exists())

    def test_name_never_overwrites_an_existing_recording(self) -> None:
        original = self.directory / "system-audio_2026-10-13_12-00-00.wav"
        existing = self.directory / "system-audio_2026-10-13_12-00-00_demo.wav"
        write_test_wav(original)
        existing.write_bytes(b"keep me")

        renamed = add_recording_name(original, "demo")

        self.assertEqual(existing.read_bytes(), b"keep me")
        self.assertEqual(renamed.stem, f"{existing.stem}_1")

    def test_recordings_are_listed_newest_first(self) -> None:
        older = self.directory / "older.wav"
        newer = self.directory / "newer.wav"
        write_test_wav(older)
        write_test_wav(newer, frames=9_600)
        os.utime(older, (100, 100))
        os.utime(newer, (200, 200))

        recordings = list_recordings(self.directory)

        self.assertEqual([item.path for item in recordings], [newer, older])
        self.assertEqual(recordings[0].duration_seconds, 0.2)

    @patch("system_audio_recorder.library.subprocess.run")
    @patch("system_audio_recorder.library.Path.is_file", return_value=True)
    def test_playback_uses_builtin_afplay(self, is_file, run) -> None:
        del is_file
        run.return_value.returncode = 0
        path = self.directory / "recording.wav"

        play_recording(path)

        run.assert_called_once_with(
            ["/usr/bin/afplay", str(path)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_missing_recording_cannot_be_played(self) -> None:
        with self.assertRaisesRegex(PlaybackError, "no longer exists"):
            play_recording(self.directory / "missing.wav")

    @patch("system_audio_recorder.library.subprocess.run")
    @patch("system_audio_recorder.library.Path.is_file", return_value=True)
    def test_open_folder_uses_builtin_open(self, is_file, run) -> None:
        del is_file
        run.return_value.returncode = 0
        recordings = self.directory / "recordings"

        open_recordings_folder(recordings)

        self.assertTrue(recordings.is_dir())
        run.assert_called_once_with(
            ["/usr/bin/open", str(recordings)],
            check=False,
            capture_output=True,
            text=True,
        )

    @patch("system_audio_recorder.library.subprocess.run")
    @patch("system_audio_recorder.library.Path.is_file", return_value=True)
    def test_open_folder_reports_finder_failure(self, is_file, run) -> None:
        del is_file
        run.return_value.returncode = 1
        run.return_value.stderr = "Finder unavailable"

        with self.assertRaisesRegex(FolderOpenError, "Finder unavailable"):
            open_recordings_folder(self.directory)


if __name__ == "__main__":
    unittest.main()
