"""Build and supervise the native ScreenCaptureKit helper."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
HELPER_SOURCE = PROJECT_ROOT / "native" / "SystemAudioCapture.swift"
BUILD_DIRECTORY = PROJECT_ROOT / ".build"
HELPER_BINARY = BUILD_DIRECTORY / "system-audio-capture"


class NativeCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class NativeCaptureResult:
    frames: int
    duration_seconds: float
    warnings: tuple[str, ...]


def ensure_native_helper() -> Path:
    """Build the helper when missing or older than its reviewed Swift source."""
    if platform.system() != "Darwin":
        raise NativeCaptureError("Native system audio capture requires macOS.")
    if not HELPER_SOURCE.exists():
        raise NativeCaptureError(f"Native helper source is missing: {HELPER_SOURCE}")
    if HELPER_BINARY.exists() and (
        HELPER_BINARY.stat().st_mtime_ns >= HELPER_SOURCE.stat().st_mtime_ns
    ):
        return HELPER_BINARY

    xcrun = shutil.which("xcrun")
    if xcrun is None:
        raise NativeCaptureError(
            "Apple developer tools are required once to build the native helper. "
            "Install Xcode Command Line Tools, then start again."
        )

    BUILD_DIRECTORY.mkdir(parents=True, exist_ok=True)
    temporary_binary = BUILD_DIRECTORY / (
        f".system-audio-capture-{os.getpid()}.building"
    )
    command = [
        xcrun,
        "swiftc",
        "-O",
        "-parse-as-library",
        "-target",
        f"{platform.machine()}-apple-macosx15.0",
        str(HELPER_SOURCE),
        "-o",
        str(temporary_binary),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if completed.returncode != 0:
        temporary_binary.unlink(missing_ok=True)
        details = completed.stderr.strip() or "The Swift compiler returned no details."
        raise NativeCaptureError(f"Could not build the native helper: {details}")
    temporary_binary.chmod(0o755)
    os.replace(temporary_binary, HELPER_BINARY)
    return HELPER_BINARY


class NativeCaptureSession:
    """A line-delimited JSON client for one native capture subprocess."""

    def __init__(self, output_path: Path, sample_rate: int) -> None:
        self.output_path = output_path
        self.sample_rate = sample_rate
        self._process: subprocess.Popen[str] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._finished = threading.Event()
        self._failure: str | None = None
        self._result: NativeCaptureResult | None = None
        self._warnings: list[str] = []
        self._stderr: deque[str] = deque(maxlen=20)
        self._lock = threading.Lock()
        self._started_at: float | None = None

    @property
    def elapsed_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    @property
    def failure(self) -> str | None:
        with self._lock:
            return self._failure

    def start(self, timeout: float = 120.0) -> None:
        helper = ensure_native_helper()
        command = [
            str(helper),
            "--output",
            str(self.output_path),
            "--sample-rate",
            str(self.sample_rate),
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise NativeCaptureError(f"Could not launch the native helper: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._read_events,
            name="native-capture-events",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            name="native-capture-stderr",
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if self._ready.wait(timeout=0.1):
                    self._started_at = time.monotonic()
                    return
                if self._finished.is_set() or self.failure is not None:
                    break
        except BaseException:
            self.abort()
            raise

        timed_out = time.monotonic() >= deadline
        error = self.failure
        if error is None and not timed_out:
            error = self._exit_details()
        self.abort()
        if timed_out and error is None:
            error = "Timed out waiting for ScreenCaptureKit permission or startup."
        raise NativeCaptureError(error or "The native helper exited before capture started.")

    def stop(self, timeout: float = 30.0) -> NativeCaptureResult:
        process = self._process
        if process is None:
            raise NativeCaptureError("The native helper is not running.")

        if process.poll() is None:
            try:
                assert process.stdin is not None
                process.stdin.write('{"command":"stop"}\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

        if not self._finished.wait(timeout=timeout):
            self.abort()
            raise NativeCaptureError("The native helper did not stop in time.")
        self._join_threads()

        if self._result is not None:
            return self._result
        raise NativeCaptureError(self.failure or self._exit_details())

    def abort(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self._finished.set()
        self._join_threads()

    def _read_events(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for raw_line in process.stdout:
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    self._set_failure("The native helper returned invalid protocol data.")
                    continue
                self._handle_event(event)
        finally:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            if self._result is None and self.failure is None:
                self._set_failure(self._exit_details())
            self._finished.set()

    def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        for raw_line in process.stderr:
            self._stderr.append(raw_line.rstrip())

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_name = event.get("event")
        if event_name == "ready":
            self._ready.set()
        elif event_name == "warning":
            message = str(event.get("message", "Native capture warning"))
            self._warnings.append(message)
        elif event_name in {"error", "interrupted"}:
            self._set_failure(str(event.get("message", "Native capture failed.")))
        elif event_name == "stopped":
            event_warnings = tuple(str(item) for item in event.get("warnings", []))
            self._result = NativeCaptureResult(
                frames=int(event.get("frames", 0)),
                duration_seconds=float(event.get("duration", 0.0)),
                warnings=tuple(self._warnings) + event_warnings,
            )

    def _set_failure(self, message: str) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = message

    def _exit_details(self) -> str:
        stderr = "\n".join(self._stderr).strip()
        process = self._process
        return_code = process.poll() if process is not None else None
        if stderr:
            return f"Native helper exited with code {return_code}: {stderr}"
        return f"Native helper exited unexpectedly with code {return_code}."

    def _join_threads(self) -> None:
        current = threading.current_thread()
        for thread in (self._reader_thread, self._stderr_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=2)
        process = self._process
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None and not pipe.closed:
                    pipe.close()

