"""MD5-only durable byte cache with fixed 1800-second expiry."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class CacheEntry:
    md5: str
    path: Path
    size: int
    created_at: float

    @property
    def expires_at(self) -> float:
        return self.created_at + 1800


class CacheStore:
    """One-process cache index. File mtime records original commit time.

    Incomplete .tmp files never become cache hits. This intentionally does not
    deduplicate two concurrent initial writes of the same MD5.
    """

    def __init__(self, root: Path, *, clock=time.time):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.entries: dict[str, CacheEntry] = {}
        self.rebuild()

    def _path(self, md5: str) -> Path:
        if not MD5_PATTERN.fullmatch(md5):
            raise ValueError("invalid md5")
        return self.root / md5[:2] / f"{md5}.part"

    def _remove(self, entry: CacheEntry) -> None:
        self.entries.pop(entry.md5, None)
        entry.path.unlink(missing_ok=True)

    def get(self, md5: str) -> CacheEntry | None:
        path = self._path(md5)
        entry = self.entries.get(md5)
        if entry is None:
            return None
        if self.clock() >= entry.expires_at or not path.is_file():
            self._remove(entry)
            return None
        return entry

    def rebuild(self) -> None:
        self.entries.clear()
        for path in self.root.glob("*/*.part"):
            try:
                if not MD5_PATTERN.fullmatch(path.stem) or path.parent.name != path.stem[:2]:
                    continue
                stat = path.stat()
                entry = CacheEntry(path.stem, path, stat.st_size, stat.st_mtime)
                if self.clock() >= entry.expires_at:
                    path.unlink(missing_ok=True)
                else:
                    self.entries[entry.md5] = entry
            except FileNotFoundError:
                continue
        # Uncommitted files are never reusable, including after restart.
        for path in self.root.glob("*/*.tmp"):
            path.unlink(missing_ok=True)

    def cleanup(self) -> int:
        count = 0
        for entry in tuple(self.entries.values()):
            if self.clock() >= entry.expires_at:
                self._remove(entry)
                count += 1
        return count

    async def put(self, md5: str, chunks: AsyncIterator[bytes], expected_size: int) -> CacheEntry:
        if expected_size < 0:
            raise ValueError("invalid size")
        path = self._path(md5)
        existing = self.get(md5)
        if existing is not None:
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        min_free = int(os.environ.get("R2_RELAY_MIN_FREE_BYTES", str(128 * 1024 * 1024)))
        available = shutil.disk_usage(path.parent).free
        if available < expected_size + min_free:
            raise OSError("VPS cache disk space below configured reserve")
        fd, name = tempfile.mkstemp(prefix=f"{md5}-", suffix=".tmp", dir=path.parent)
        tmp = Path(name)
        size = 0
        try:
            with os.fdopen(fd, "wb") as out:
                async for data in chunks:
                    size += len(data)
                    if size > expected_size:
                        raise ValueError("body exceeds declared length")
                    await asyncio.to_thread(out.write, data)
                if size != expected_size:
                    raise ValueError("body length mismatch")
                await asyncio.to_thread(out.flush)
                await asyncio.to_thread(os.fsync, out.fileno())
            created = self.clock()
            # An existing complete cache is not refreshed by a repeated write.
            existing = self.get(md5)
            if existing is not None:
                return existing
            os.utime(tmp, (created, created))
            os.replace(tmp, path)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            entry = CacheEntry(md5, path, size, created)
            self.entries[md5] = entry
            return entry
        finally:
            tmp.unlink(missing_ok=True)
