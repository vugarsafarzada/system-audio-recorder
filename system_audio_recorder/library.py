"""Recording naming, discovery, and playback helpers."""

from __future__ import annotations

import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from .output import publish_without_overwrite


class PlaybackError(RuntimeError):
    pass


class FolderOpenError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecordingEntry:
    path: Path
    modified_time: float
    size_bytes: int
    duration_seconds: float | None


def add_recording_name(path: Path, name: str) -> Path:
    """Append a safe user-provided name without replacing another recording."""
    safe_name = _safe_name(name)
    if not safe_name:
        return path
    desired = path.with_name(f"{path.stem}_{safe_name}{path.suffix}")
    return publish_without_overwrite(path, desired)


def list_recordings(directory: Path) -> list[RecordingEntry]:
    """Return WAV recordings newest first, skipping transient files."""
    entries: list[RecordingEntry] = []
    if not directory.exists():
        return entries

    for path in directory.glob("*.wav"):
        if path.name.startswith("."):
            continue
        try:
            if not path.is_file():
                continue
            details = path.stat()
        except OSError:
            continue
        entries.append(
            RecordingEntry(
                path=path,
                modified_time=details.st_mtime,
                size_bytes=details.st_size,
                duration_seconds=_wav_duration(path),
            )
        )

    entries.sort(
        key=lambda entry: (entry.modified_time, entry.path.name), reverse=True
    )
    return entries


def play_recording(path: Path) -> None:
    """Play one WAV with macOS's built-in audio player."""
    if not path.is_file():
        raise PlaybackError(f"Recording no longer exists: {path}")
    player = Path("/usr/bin/afplay")
    if not player.is_file():
        raise PlaybackError("The built-in macOS audio player was not found.")

    try:
        completed = subprocess.run(
            [str(player), str(path)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PlaybackError(f"Could not start playback: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise PlaybackError(f"Playback failed: {detail}")


def open_recordings_folder(directory: Path) -> None:
    """Create the recordings directory if needed and reveal it in Finder."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise FolderOpenError(
            f"Could not create the recordings folder: {exc}"
        ) from exc

    opener = Path("/usr/bin/open")
    if not opener.is_file():
        raise FolderOpenError("The built-in macOS open command was not found.")
    try:
        completed = subprocess.run(
            [str(opener), str(directory)],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FolderOpenError(f"Could not open the recordings folder: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit code {completed.returncode}"
        raise FolderOpenError(f"Could not open the recordings folder: {detail}")


def _safe_name(name: str, maximum_length: int = 80) -> str:
    pieces: list[str] = []
    previous_separator = False
    for character in name.strip():
        if character.isalnum() or character in {"-", "_"}:
            pieces.append(character)
            previous_separator = False
        elif not previous_separator:
            pieces.append("-")
            previous_separator = True
    return "".join(pieces).strip("-_")[:maximum_length].rstrip("-_")


def _wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as recording:
            sample_rate = recording.getframerate()
            if sample_rate <= 0:
                return None
            return recording.getnframes() / sample_rate
    except (OSError, EOFError, wave.Error):
        return None
