"""Content-addressed artifact store (sha256 blobs on disk + SQLite WAL index).

Handles look like ``artifact://sha256:<hex>``. Blobs are immutable; the index
records provenance (session, kind, taint). Everything is on disk, so artifacts
survive daemon and worker restarts.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PREFIX = "artifact://sha256:"
_CHUNK = 1 << 20


@dataclass(frozen=True)
class ArtifactInfo:
    handle: str
    size: int
    kind: str
    session: str
    taint: str
    created: float


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        (self.root / "blobs").mkdir(parents=True, exist_ok=True)
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "index.sqlite3", isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS artifacts (handle TEXT, session TEXT, kind TEXT,"
            " taint TEXT, size INTEGER, created REAL, PRIMARY KEY (handle, session, kind))"
        )

    # -- paths ---------------------------------------------------------------
    @staticmethod
    def digest_of(handle: str) -> str:
        if not handle.startswith(PREFIX):
            raise ValueError(f"not an artifact handle: {handle!r}")
        digest = handle[len(PREFIX):]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"malformed artifact digest: {digest!r}")
        return digest

    def blob_path(self, handle: str) -> Path:
        d = self.digest_of(handle)
        return self.root / "blobs" / d[:2] / d

    # -- writes --------------------------------------------------------------
    def _commit(self, tmp: Path, digest: str, size: int, kind: str, session: str, taint: str) -> str:
        handle = PREFIX + digest
        dest = self.blob_path(handle)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            if dest.exists():
                tmp.unlink(missing_ok=True)
            else:
                os.replace(tmp, dest)
                os.chmod(dest, 0o444)
            self.db.execute(
                "INSERT OR IGNORE INTO artifacts VALUES (?,?,?,?,?,?)",
                (handle, session, kind, taint, size, time.time()),
            )
            return handle

    def put_bytes(self, data: bytes, *, kind: str = "blob", session: str = "", taint: str = "internal") -> str:
        fd, name = tempfile.mkstemp(dir=self.root / "tmp")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return self._commit(Path(name), hashlib.sha256(data).hexdigest(), len(data), kind, session, taint)

    def put_file(self, src: Path, *, kind: str = "blob", session: str = "", taint: str = "internal") -> str:
        """Stream-copy a file into the store without loading it into memory."""
        h = hashlib.sha256()
        size = 0
        fd, name = tempfile.mkstemp(dir=self.root / "tmp")
        with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
            while chunk := inp.read(_CHUNK):
                h.update(chunk)
                out.write(chunk)
                size += len(chunk)
        return self._commit(Path(name), h.hexdigest(), size, kind, session, taint)

    # -- reads ---------------------------------------------------------------
    def exists(self, handle: str) -> bool:
        return self.blob_path(handle).exists()

    def drop(self, handle: str, session: str | None = None) -> None:
        """Remove index row(s) for a handle (blob stays; content is immutable
        and may be shared). Used by session state drop."""
        with self._lock:
            if session is None:
                self.db.execute("DELETE FROM artifacts WHERE handle=?", (handle,))
            else:
                self.db.execute("DELETE FROM artifacts WHERE handle=? AND session=?",
                                (handle, session))

    def read_bytes(self, handle: str, offset: int = 0, length: int | None = None) -> bytes:
        with open(self.blob_path(handle), "rb") as fh:
            fh.seek(offset)
            return fh.read() if length is None else fh.read(length)

    def read_lines(self, handle: str, start: int, end: int) -> list[str]:
        """1-based inclusive line range, streamed."""
        out: list[str] = []
        with open(self.blob_path(handle), "r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh, start=1):
                if i > end:
                    break
                if i >= start:
                    out.append(line.rstrip("\n"))
        return out

    def info(self, handle: str) -> list[ArtifactInfo]:
        rows = self.db.execute(
            "SELECT handle,size,kind,session,taint,created FROM artifacts WHERE handle=?", (handle,)
        ).fetchall()
        return [ArtifactInfo(*r) for r in rows]

    def list(self, session: str | None = None) -> list[ArtifactInfo]:
        q = "SELECT handle,size,kind,session,taint,created FROM artifacts"
        args: tuple[str, ...] = ()
        if session is not None:
            q += " WHERE session=?"
            args = (session,)
        return [ArtifactInfo(*r) for r in self.db.execute(q + " ORDER BY created", args)]

    def close(self) -> None:
        self.db.close()
