"""Interactive terminal interface."""

from __future__ import annotations

import select
import signal
import sys
import termios
import tty
from pathlib import Path

from .library import (
    PlaybackError,
    add_recording_name,
    list_recordings,
    play_recording,
)
from .recorder import (
    EmptyRecordingError,
    RecordingConfig,
    RecordingError,
    SystemAudioRecorder,
)


def format_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3_600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def choose_format() -> str:
    while True:
        answer = input("Output format [WAV only] (press Return): ").strip().casefold()
        if answer in {"", "1", "wav", "w"}:
            return "wav"
        if answer in {"2", "mp3", "m"}:
            print("MP3 is unavailable because macOS does not provide a native encoder.")
            continue
        print("WAV is the only dependency-free native format enabled.")


def choose_sample_rate() -> int:
    while True:
        answer = input("Sample rate [1=48000 Hz, 2=44100 Hz] (default 1): ").strip()
        if answer in {"", "1", "48000", "48"}:
            return 48_000
        if answer in {"2", "44100", "44.1"}:
            return 44_100
        print("Choose 1 for 48000 Hz or 2 for 44100 Hz.")


def wait_for_stop(recorder: SystemAudioRecorder) -> None:
    if not sys.stdin.isatty():
        _wait_for_stop_from_pipe(recorder)
        return

    print("Recording started. Press S or Return to stop. Ctrl+C also works.")
    descriptor = sys.stdin.fileno()
    original_settings = termios.tcgetattr(descriptor)
    last_duration = ""
    try:
        tty.setcbreak(descriptor)
        while True:
            failure = recorder.failure
            if failure:
                _clear_status_line()
                print(f"Capture interruption: {failure}")
                return
            duration = format_duration(recorder.elapsed_seconds)
            if duration != last_duration:
                print(
                    f"\rRecording {duration} | Press S or Return to stop",
                    end="",
                    flush=True,
                )
                last_duration = duration
            readable, _, _ = select.select([sys.stdin], [], [], 0.2)
            if readable:
                key = sys.stdin.read(1)
                if key in {"", "\r", "\n", "s", "S"}:
                    return
    except KeyboardInterrupt:
        _clear_status_line()
        print("Interrupt received. Finalizing the recording...")
    finally:
        termios.tcflush(descriptor, termios.TCIFLUSH)
        termios.tcsetattr(descriptor, termios.TCSADRAIN, original_settings)
        _clear_status_line()


def _wait_for_stop_from_pipe(recorder: SystemAudioRecorder) -> None:
    print("Recording started. Send 'stop' and Return, or send EOF.")
    while True:
        failure = recorder.failure
        if failure:
            print(f"Capture interruption: {failure}")
            return
        readable, _, _ = select.select([sys.stdin], [], [], 0.2)
        if readable:
            command = sys.stdin.readline()
            if command == "" or command.strip().casefold() in {"stop", "s", ""}:
                return


def _clear_status_line() -> None:
    print("\r\033[2K", end="", flush=True)


def record_once(output_directory: Path) -> None:
    try:
        output_format = choose_format()
        sample_rate = choose_sample_rate()
    except (EOFError, KeyboardInterrupt):
        print("\nRecording cancelled before capture started.")
        return

    recorder = SystemAudioRecorder(
        RecordingConfig(
            sample_rate=sample_rate,
            output_format=output_format,
            output_directory=output_directory,
        )
    )

    print(
        "Starting Apple's system audio capture. macOS may request "
        "Screen & System Audio Recording permission."
    )
    try:
        recorder.start()
        wait_for_stop(recorder)
        result = recorder.stop()
    except EmptyRecordingError as exc:
        print(f"No recording saved: {exc}")
        return
    except RecordingError as exc:
        print(f"Recording error: {exc}")
        return

    final_path = _prompt_for_recording_name(result.path)
    duration = format_duration(result.duration_seconds)
    print(f"Saved WAV recording: {final_path} ({duration})")
    for warning in result.warnings:
        print(f"Warning: {warning}")


def _prompt_for_recording_name(path: Path) -> Path:
    try:
        name = input("Recording name (optional, press Return to keep timestamp): ")
    except (EOFError, KeyboardInterrupt):
        print("\nKeeping the timestamped filename.")
        return path
    if not name.strip():
        return path
    try:
        named_path = add_recording_name(path, name)
    except OSError as exc:
        print(f"Could not apply that name; keeping '{path.name}': {exc}")
        return path
    if named_path == path:
        print("That name had no usable filename characters; keeping the timestamp.")
    return named_path


def show_recordings(output_directory: Path) -> None:
    while True:
        recordings = list_recordings(output_directory)
        if not recordings:
            print("No recordings found.")
            return

        print("\nRecordings (newest first):")
        for index, recording in enumerate(recordings, start=1):
            duration = (
                format_duration(recording.duration_seconds)
                if recording.duration_seconds is not None
                else "unknown"
            )
            size = _format_file_size(recording.size_bytes)
            print(f"[{index}] {recording.path.name} | {duration} | {size}")

        try:
            answer = input(
                "Choose a number to play, or press Return to go back: "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not answer:
            return
        if not answer.isdigit() or not 1 <= int(answer) <= len(recordings):
            print("Choose one of the listed recording numbers.")
            continue

        selected = recordings[int(answer) - 1]
        print(f"Playing {selected.path.name}. Press Ctrl+C to stop playback.")
        try:
            play_recording(selected.path)
        except KeyboardInterrupt:
            print("\nPlayback stopped.")
        except PlaybackError as exc:
            print(f"Playback error: {exc}")


def _format_file_size(size_bytes: int) -> str:
    if size_bytes < 1_024:
        return f"{size_bytes} B"
    if size_bytes < 1_024 * 1_024:
        return f"{size_bytes / 1_024:.1f} KB"
    return f"{size_bytes / (1_024 * 1_024):.1f} MB"


def _termination_handler(signum: int, frame: object) -> None:
    del signum, frame
    raise KeyboardInterrupt


def main() -> int:
    if sys.platform != "darwin":
        print("This recorder supports macOS only.")
        return 1

    signal.signal(signal.SIGTERM, _termination_handler)
    output_directory = Path(__file__).resolve().parent.parent / "recordings"
    print("macOS System Audio Recorder")
    print("Uses ScreenCaptureKit for system audio only. Microphone capture is disabled.")

    while True:
        print("\n[1] Start  [2] Stop  [3] Recordings  [4] Exit")
        try:
            command = input("> ").strip().casefold()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return 0

        if command in {"1", "start"}:
            record_once(output_directory)
        elif command in {"2", "stop"}:
            print("No recording is active. Choose Start first.")
        elif command in {"3", "recordings", "list", "play"}:
            show_recordings(output_directory)
        elif command in {"4", "exit", "quit", "q"}:
            print("Goodbye.")
            return 0
        else:
            print("Choose Start, Stop, Recordings, or Exit.")


if __name__ == "__main__":
    raise SystemExit(main())

