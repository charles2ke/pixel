"""Stream ZIP archives of photos and videos while they are read from GitHub.

Members are stored uncompressed (photos and videos are compressed already)
and written with data descriptors, so an archive of any size is produced with
a few hundred kilobytes of memory and the download starts right away.
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator, List, Optional, Sequence, Tuple

from gitdb import NotFoundError

#: Buffered bytes handed to the HTTP response at a time.
FLUSH_AT = 256 * 1024
MAX_NAME = 150
MISSING_NOTE = "MISSING.txt"

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{port}{n}" for port in ("COM", "LPT") for n in range(1, 10)}


def safe_name(name: str, fallback: str = "download") -> str:
    """Return ``name`` as a file name that Windows, macOS and Linux all accept."""
    cleaned = _UNSAFE.sub("_", name or "").strip(" .")
    if len(cleaned) > MAX_NAME:
        stem, dot, extension = cleaned.rpartition(".")
        if dot and stem and len(extension) <= 10:
            cleaned = stem[: MAX_NAME - len(extension) - 1].rstrip(" .") + "." + extension
        else:
            cleaned = cleaned[:MAX_NAME].rstrip(" .")
    if not cleaned or cleaned.startswith("."):
        cleaned = (fallback + cleaned) if cleaned else fallback
    if cleaned.split(".", 1)[0].upper() in _RESERVED:
        cleaned = "_" + cleaned
    return cleaned


def unique_names(names: Iterable[str]) -> List[str]:
    """Number repeated names (``photo (2).jpg``) so no member replaces another.

    Names are compared case-insensitively, as Windows and macOS do.
    """
    taken = set()
    unique: List[str] = []
    for name in names:
        stem, dot, extension = name.rpartition(".")
        if not dot or not stem:
            stem, extension = name, ""
        suffix = f".{extension}" if extension else ""
        candidate, number = name, 2
        while candidate.lower() in taken:
            candidate = f"{stem} ({number}){suffix}"
            number += 1
        taken.add(candidate.lower())
        unique.append(candidate)
    return unique


def zip_time(value: Optional[str]) -> Tuple[int, int, int, int, int, int]:
    """Convert an RFC 3339 timestamp to a ZIP member time (UTC)."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    # The ZIP format cannot represent times outside 1980-2107.
    if moment.year < 1980:
        return (1980, 1, 1, 0, 0, 0)
    if moment.year > 2107:
        return (2107, 12, 31, 23, 59, 58)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


@dataclass(frozen=True)
class Entry:
    """One archive member. ``fetch`` opens its bytes and raises
    :class:`NotFoundError` when the file cannot be read."""

    name: str
    size: Optional[int]
    modified: Optional[str]
    fetch: Callable[[], Iterator[bytes]]


class _Sink(io.RawIOBase):
    """Collects what :mod:`zipfile` writes so it can be streamed out in chunks.

    It cannot seek, so :mod:`zipfile` writes sizes and checksums after each
    member instead of going back to patch its header.
    """

    def __init__(self) -> None:
        super().__init__()
        self._chunks: List[bytes] = []
        self.buffered = 0
        self._offset = 0

    def writable(self) -> bool:
        return True

    def write(self, data: bytes) -> int:  # type: ignore[override]
        chunk = bytes(data)
        self._chunks.append(chunk)
        self.buffered += len(chunk)
        self._offset += len(chunk)
        return len(chunk)

    def tell(self) -> int:
        return self._offset

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        self.buffered = 0
        return data


def stream_zip(entries: Sequence[Entry]) -> Iterator[bytes]:
    """Yield a ZIP archive of ``entries`` chunk by chunk.

    Files GitHub no longer serves are left out and listed in a note inside the
    archive instead of failing a download that is already under way.
    """
    sink = _Sink()
    missing: List[str] = []
    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for entry in entries:
            try:
                chunks = entry.fetch()
            except NotFoundError:
                missing.append(entry.name)
                continue
            info = zipfile.ZipInfo(entry.name, date_time=zip_time(entry.modified))
            info.compress_type = zipfile.ZIP_STORED
            info.external_attr = 0o644 << 16
            if isinstance(entry.size, int) and entry.size > 0:
                # Lets zipfile choose ZIP64 up front for very large members.
                info.file_size = entry.size
            try:
                with archive.open(info, mode="w") as member:
                    for chunk in chunks:
                        member.write(chunk)
                        if sink.buffered >= FLUSH_AT:
                            yield sink.drain()
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()
        if missing:
            note = unique_names([entry.name for entry in entries] + [MISSING_NOTE])[-1]
            text = "These files could not be read from GitHub and are not in this archive:\n\n"
            info = zipfile.ZipInfo(note, date_time=zip_time(None))
            info.external_attr = 0o644 << 16
            archive.writestr(info, text + "\n".join(missing) + "\n")
    yield sink.drain()
