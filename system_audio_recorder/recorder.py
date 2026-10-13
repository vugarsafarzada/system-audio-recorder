"""System audio recording lifecycle backed by ScreenCaptureKit."""

from __future__ import annotations

import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .native import NativeCaptureError, NativeCaptureResult, NativeCaptureSession
from .output import publish_without_overwrite, temporary_wav_path, timestamped_path


class RecordingError(RuntimeError):
    pass


class EmptyRecordingError(RecordingError):
    pass


class CaptureSession(Protocol):
    @property
    def elapsed_seconds(self) -> float: ...

    @property
    def failure(self) -> str | None: ...

    def start(self) -> None: ...

    def stop(self) -> NativeCaptureResult: ...

    def abort(self) -> None: ...


CaptureFactory = Callable[[Path, int], CaptureSession]


@dataclass(frozen=True)
class RecordingConfig:
    sample_rate: int = 48_000
    output_format: str = "wav"
    output_directory: Path = Path("recordings")

    def __post_init__(self) -> None:
        normalized = self.output_format.casefold()
        if normalized != "wav":
            raise ValueError(
                "Only WAV is supported. This macOS installation has no native "
                "MP3 encoder."
            )
        if self.sample_rate not in {44_100, 48_000}:
            raise ValueError("Sample rate must be 44100 or 48000 Hz.")
        object.__setattr__(self, "output_format", normalized)


@dataclass(frozen=True)
class RecordingResult:
    path: Path
    duration_seconds: float
    requested_format: str = "wav"
    actual_format: str = "wav"
    warnings: tuple[str, ...] = field(default_factory=tuple)


class SystemAudioRecorder:
    def __init__(
        self,
        config: RecordingConfig,
        *,
        capture_factory: CaptureFactory = NativeCaptureSession,
    ) -> None:
        self.config = config
        self.capture_factory = capture_factory
        self._session: CaptureSession | None = None
        self._staging_path: Path | None = None
        self._running = False
        self._started_at: float | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def elapsed_seconds(self) -> float:
        if self._session is not None:
            return self._session.elapsed_seconds
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def failure(self) -> str | None:
        return self._session.failure if self._session is not None else None

    def start(self) -> None:
        if self._running:
            raise RecordingError("A recording is already in progress.")

        try:
            self.config.output_directory.mkdir(parents=True, exist_ok=True)
            self._staging_path = temporary_wav_path(self.config.output_directory)
            self._session = self.capture_factory(
                self._staging_path, self.config.sample_rate
            )
            self._session.start()
            self._started_at = time.monotonic()
            self._running = True
        except BaseException as exc:
            self._abort_session()
            self._remove_staging_file()
            if isinstance(exc, RecordingError):
                raise
            raise RecordingError(str(exc)) from exc

    def stop(self) -> RecordingResult:
        if not self._running or self._session is None:
            raise RecordingError("No recording is in progress.")

        self._running = False
        session = self._session
        try:
            native_result = session.stop()
        except NativeCaptureError as exc:
            self._session = None
            return self._recover_or_raise(exc)
        except Exception as exc:
            self._session = None
            return self._recover_or_raise(NativeCaptureError(str(exc)))
        self._session = None

        if self._staging_path is None or native_result.frames <= 0:
            self._remove_staging_file()
            raise EmptyRecordingError(
                "No system audio frames were captured, so no empty file was saved."
            )

        try:
            wav_duration = self._validate_wav(self._staging_path)
        except RecordingError:
            self._remove_staging_file()
            raise
        final_path = self._publish(self._staging_path)
        self._staging_path = None
        return RecordingResult(
            path=final_path,
            duration_seconds=wav_duration,
            warnings=native_result.warnings,
        )

    def abort(self) -> None:
        """Stop an active helper and remove an unusable temporary file."""
        if not self._running:
            return
        try:
            self.stop()
        except RecordingError:
            self._abort_session()
            self._remove_staging_file()

    def _recover_or_raise(self, error: NativeCaptureError) -> RecordingResult:
        if self._staging_path is not None and self._staging_path.exists():
            try:
                duration = self._validate_wav(self._staging_path)
            except RecordingError:
                self._remove_staging_file()
            else:
                final_path = self._publish(self._staging_path)
                self._staging_path = None
                return RecordingResult(
                    path=final_path,
                    duration_seconds=duration,
                    warnings=(f"Capture ended unexpectedly: {error}",),
                )
        raise RecordingError(str(error)) from error

    def _publish(self, staging_path: Path) -> Path:
        try:
            desired = timestamped_path(self.config.output_directory)
            return publish_without_overwrite(staging_path, desired)
        except OSError as exc:
            raise RecordingError(
                f"The WAV was captured but could not be published. "
                f"It remains at '{staging_path}': {exc}"
            ) from exc

    def _validate_wav(self, path: Path) -> float:
        try:
            with wave.open(str(path), "rb") as recording:
                channels = recording.getnchannels()
                sample_width = recording.getsampwidth()
                sample_rate = recording.getframerate()
                frames = recording.getnframes()
        except (OSError, EOFError, wave.Error) as exc:
            raise RecordingError(f"The native helper produced an invalid WAV: {exc}") from exc

        if channels != 2 or sample_width != 2:
            raise RecordingError(
                f"Unexpected WAV format: {channels} channels, "
                f"{sample_width * 8}-bit samples."
            )
        if sample_rate != self.config.sample_rate:
            raise RecordingError(f"Unexpected WAV sample rate: {sample_rate} Hz.")
        if frames <= 0:
            raise EmptyRecordingError("The native helper produced an empty WAV.")
        return frames / sample_rate

    def _abort_session(self) -> None:
        if self._session is not None:
            try:
                self._session.abort()
            finally:
                self._session = None
        self._running = False

    def _remove_staging_file(self) -> None:
        if self._staging_path is not None:
            self._staging_path.unlink(missing_ok=True)
            self._staging_path = None

