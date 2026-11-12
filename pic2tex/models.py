"""Shared data models for Pic2TeX."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ImagePayload:
    """An encoded image plus the metadata the app needs to display/send it."""

    data: bytes
    mime: str
    width: int
    height: int
    source: str = "file"
    name: str | None = None

    @property
    def size(self) -> int:
        """Encoded size in bytes."""
        return len(self.data)

    @property
    def size_human(self) -> str:
        size = float(len(self.data))
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{len(self.data)} B"
