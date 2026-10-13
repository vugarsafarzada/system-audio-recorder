"""Safe output naming and publication."""

from __future__ import annotations

import os
import uuid
from datetime import datetime
from pathlib import Path


def timestamped_path(directory: Path, extension: str = "wav") -> Path:
    stem = datetime.now().strftime("system-audio_%Y-%m-%d_%H-%M-%S")
    candidate = directory / f"{stem}.{extension}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"{stem}_{counter}.{extension}"
        counter += 1
    return candidate


def temporary_wav_path(directory: Path) -> Path:
    return directory / f".recording-{uuid.uuid4().hex}.part.wav"


def publish_without_overwrite(source: Path, desired: Path) -> Path:
    """Atomically publish source without replacing an existing file."""
    desired.parent.mkdir(parents=True, exist_ok=True)
    target = desired
    counter = 1
    while True:
        try:
            os.link(source, target)
            source.unlink()
            return target
        except FileExistsError:
            target = desired.with_name(f"{desired.stem}_{counter}{desired.suffix}")
            counter += 1

