"""Fast project intake and source ledger (§19.5, §19.6).

Architectural difference from Local Harness:
  - Intake is FAST ($O(N)$ streaming sha256 metadata check). Does NOT block on eager
    deep extraction or OCR during directory inventory.
  - Extraction is LAZY: runs on-demand when requested by model or workflow stage.
  - Extracted results are stored in ArtifactStore (sha256 content-addressed).
  - Unmodified files reuse existing artifact handles in $O(1)$.
  - Incorporates Gen 1.6 source-role classification and reviewability states.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keel.daemon.artifacts import ArtifactStore
from keel.docs.extract import extract_document
from keel.policy import taint

# Gen 1.6 role classification heuristics
NONAUTH_ROLES = (
    ("archive_superseded", ("archive", "superseded", "old versions", "/old/", "deprecated")),
    ("generated_output", ("outputs/", "/output/", "qa_report", "generated")),
    ("candidate_draft", ("maintainer_build", "candidate", "draft_", "_draft", "wip")),
    ("state_artifact", ("state_artifacts", "restart", "checkpoint")),
)

AUTH_ROLES = (
    ("governing_document", ("declaration", "bylaw", "covenant", "amendment", "articles of", "restated", "plat")),
    ("pleading_or_order", ("complaint", "answer", "motion", "order", "summons", "counterclaim", "petition", "writ")),
    ("discovery", ("interrogator", "request for production", "rpd", "rfa", "admission", "deposition", "subpoena")),
    ("correspondence", (".msg", ".eml", "letter", "email", "demand")),
    ("ledger_or_financial", ("ledger", "invoice", "statement", ".xlsx", ".xls", ".csv", "payoff")),
    ("authority_or_research", ("opinion", "memo", "brief", "statute", "ocga", "sb 406")),
    ("logical_file", (".json.txt", "logical")),
)

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "$RECYCLE.BIN", "System Volume Information"}


def classify_role(rel_path: str) -> str:
    norm = "/" + rel_path.lower().replace("\\", "/")
    for role, pats in NONAUTH_ROLES:
        if any(p in norm for p in pats):
            return role
    for role, pats in AUTH_ROLES:
        if any(p in norm for p in pats):
            return role
    return "other"


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class SourceRecord:
    sid: str
    rel_path: str
    abs_path: str
    size: int
    mtime: float
    sha256: str
    role: str
    state: str
    extracted_handle: str | None


def sanitize_project_id(project_id: str) -> str:
    """Sanitize a project identifier into a safe single directory name."""
    if not isinstance(project_id, str):
        raise ValueError("project_id must be a string")
    raw = project_id.strip()
    if not raw:
        raise ValueError("project_id must not be empty")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    safe = re.sub(r"\.{2,}", "_", safe)
    safe = safe.strip("._-")
    if not safe:
        raise ValueError(f"Invalid project_id: {project_id!r}")
    return safe


def normalize_fs_path(raw: Path | str, base_dir: Path | str | None = None) -> Path:
    """Normalize a local filesystem path without dereferencing leaf symlinks/junctions.

    Handles surrounding quotes (e.g. Windows Copy-as-path), bare Windows drive letters
    such as ``F:`` -> ``F:\\``, and relative paths anchored to ``base_dir``.
    """
    s = str(raw).strip()
    if len(s) >= 2 and ((s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'")):
        s = s[1:-1].strip()
    if os.name == "nt" and len(s) == 2 and s[1] == ":" and s[0].isalpha():
        s = s + "\\"
    p = Path(s) if s else Path(".")
    if not p.is_absolute() and base_dir is not None:
        base_str = str(base_dir).strip()
        if os.name == "nt" and len(base_str) == 2 and base_str[1] == ":" and base_str[0].isalpha():
            base_str = base_str + "\\"
        p = Path(base_str) / p
    return Path(os.path.abspath(str(p)))


def path_is_within(candidate: Path | str, root: Path | str) -> bool:
    """Return True if normalized ``candidate`` is equal to or under ``root``."""
    try:
        cand_norm = os.path.normcase(str(normalize_fs_path(candidate)))
        root_norm = os.path.normcase(str(normalize_fs_path(root)))
        return Path(cand_norm).is_relative_to(Path(root_norm))
    except Exception:
        return False


class SourceLedger:
    def __init__(self, root: Path | str, project_id: str | None = None) -> None:
        self.root = Path(root)
        self.project_id = project_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "sources.sqlite3", isolation_level=None, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS sources ("
            " sid TEXT PRIMARY KEY,"
            " rel_path TEXT,"
            " abs_path TEXT,"
            " size INTEGER,"
            " mtime REAL,"
            " sha256 TEXT,"
            " role TEXT,"
            " state TEXT,"
            " extracted_handle TEXT,"
            " scanned_at REAL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sources_abs_path ON sources(abs_path)")

    @classmethod
    def for_project(cls, state_dir: Path | str, project_id: str) -> "SourceLedger":
        """Create a project-scoped SourceLedger under <state_dir>/projects/<safe_pid>/sources."""
        safe_pid = sanitize_project_id(project_id)
        projects_root = (Path(state_dir).resolve() / "projects").resolve()
        ledger_root = (projects_root / safe_pid / "sources").resolve()
        if not ledger_root.is_relative_to(projects_root):
            raise ValueError(f"Unsafe project_id path traversal: {project_id!r}")
        return cls(ledger_root, project_id=project_id)

    def close(self) -> None:
        self.db.close()

    def _next_sid_number(self, prefix: str) -> int:
        rows = self.db.execute("SELECT sid FROM sources WHERE sid LIKE ?", (f"{prefix}%",)).fetchall()
        max_num = 0
        plen = len(prefix)
        for (sid,) in rows:
            if isinstance(sid, str) and sid.startswith(prefix):
                suffix = sid[plen:]
                if suffix.isdigit():
                    num = int(suffix)
                    if num > max_num:
                        max_num = num
        return max_num + 1

    def scan_directory(self, folder: Path, prefix: str = "S") -> dict[str, Any]:
        """Fast metadata inventory: scans directory, computes hashes, tags roles."""
        folder = normalize_fs_path(folder)
        if not folder.is_dir():
            raise NotADirectoryError(f"folder {folder} does not exist or is not a directory")

        count_scanned = 0
        count_new = 0
        count_updated = 0
        skipped_items: list[dict[str, str]] = []
        visited_dirs: set[tuple[int, int]] = set()
        # (rel_p, abs_str, resolved_str, size, mtime, file_sha, role)
        candidates: list[tuple[str, str, str, int, float, str, str]] = []
        cur_time = time.time()

        def _on_walk_error(err: OSError) -> None:
            skipped_items.append({
                "path": str(getattr(err, "filename", "") or ""),
                "error": f"{type(err).__name__}: {err}",
            })

        # Walk, stat, and hash OUTSIDE self._lock and SQLite transaction so concurrent
        # UI/API reads (e.g. /api/status) never block behind directory hashing I/O.
        for root_dir, dirs, files in os.walk(folder, onerror=_on_walk_error):
            try:
                dir_st = os.stat(root_dir)
                dir_key = (dir_st.st_dev, dir_st.st_ino)
                if dir_key != (0, 0) and dir_key in visited_dirs:
                    dirs[:] = []
                    continue
                if dir_key != (0, 0):
                    visited_dirs.add(dir_key)
            except OSError:
                pass

            kept_dirs: list[str] = []
            for d in sorted(dirs):
                if d in SKIP_DIRS or d.startswith("."):
                    continue
                sub_p = os.path.join(root_dir, d)
                try:
                    sub_st = os.stat(sub_p)
                    sub_key = (sub_st.st_dev, sub_st.st_ino)
                    if sub_key != (0, 0) and sub_key in visited_dirs:
                        continue
                except OSError as exc:
                    skipped_items.append({"path": sub_p, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                kept_dirs.append(d)
            dirs[:] = kept_dirs

            for fname in sorted(files):
                if fname.startswith(("~$", ".")) or fname in ("Thumbs.db", "desktop.ini"):
                    continue

                # Keep the logical path anchored under `folder` so symlinks/reparse points
                # targeting external folders or another drive do not crash relative_to(folder).
                logical_p = Path(os.path.abspath(os.path.join(root_dir, fname)))
                abs_str = str(logical_p)
                try:
                    st = logical_p.stat()
                    file_sha = compute_file_sha256(logical_p)
                except OSError as exc:
                    skipped_items.append({"path": abs_str, "error": f"{type(exc).__name__}: {exc}"})
                    continue

                try:
                    resolved_str = str(logical_p.resolve())
                except OSError:
                    resolved_str = abs_str

                try:
                    rel_p = logical_p.relative_to(folder).as_posix()
                except ValueError:
                    try:
                        rel_p = Path(os.path.relpath(abs_str, str(folder))).as_posix()
                    except ValueError:
                        rel_p = fname
                role = classify_role(rel_p)
                candidates.append((rel_p, abs_str, resolved_str, st.st_size, st.st_mtime, file_sha, role))

        lookup_sql = (
            "SELECT sid, sha256, rel_path, abs_path, extracted_handle FROM sources "
            "WHERE abs_path=? COLLATE NOCASE OR abs_path=? COLLATE NOCASE LIMIT 1"
            if os.name == "nt"
            else "SELECT sid, sha256, rel_path, abs_path, extracted_handle FROM sources "
            "WHERE abs_path=? OR abs_path=? LIMIT 1"
        )

        with self._lock:
            next_num = self._next_sid_number(prefix)
            self.db.execute("BEGIN")
            try:
                for rel_p, abs_str, resolved_str, fsize, fmtime, file_sha, role in candidates:
                    # Match either logical abs_str or legacy .resolve()d path (case-insensitive on nt)
                    # so existing sources.sqlite3 rows migrate in place without duplicate SIDs.
                    row = self.db.execute(lookup_sql, (abs_str, resolved_str)).fetchone()

                    if row is None:
                        count_scanned += 1
                        count_new += 1
                        sid = f"{prefix}{next_num:04d}"
                        next_num += 1
                        self.db.execute(
                            "INSERT INTO sources VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (sid, rel_p, abs_str, fsize, fmtime, file_sha, role, "UNEXTRACTED", None, cur_time),
                        )
                    else:
                        count_scanned += 1
                        existing_sid, existing_sha, existing_rel, existing_abs, existing_handle = row
                        if existing_sha != file_sha:
                            count_updated += 1
                            self.db.execute(
                                "UPDATE sources SET rel_path=?, abs_path=?, sha256=?, size=?, mtime=?, role=?, state=?, extracted_handle=?, scanned_at=? WHERE sid=?",
                                (rel_p, abs_str, file_sha, fsize, fmtime, role, "UNEXTRACTED", None, cur_time, existing_sid),
                            )
                        elif existing_rel != rel_p or existing_abs != abs_str:
                            self.db.execute(
                                "UPDATE sources SET rel_path=?, abs_path=?, size=?, mtime=?, role=?, scanned_at=? WHERE sid=?",
                                (rel_p, abs_str, fsize, fmtime, role, cur_time, existing_sid),
                            )
                self.db.execute("COMMIT")
            except Exception:
                try:
                    self.db.execute("ROLLBACK")
                except Exception:
                    pass
                raise

        result: dict[str, Any] = {
            "scanned": count_scanned,
            "new": count_new,
            "updated": count_updated,
        }
        if skipped_items:
            result["skipped"] = len(skipped_items)
            result["skipped_samples"] = skipped_items[:10]
        return result

    def list_sources(self, role: str | None = None) -> list[SourceRecord]:
        q = "SELECT sid, rel_path, abs_path, size, mtime, sha256, role, state, extracted_handle FROM sources"
        args: list[Any] = []
        if role:
            q += " WHERE role=?"
            args.append(role)
        q += " ORDER BY sid"
        with self._lock:
            rows = self.db.execute(q, args).fetchall()
        return [SourceRecord(*r) for r in rows]

    def get_source(self, sid: str) -> SourceRecord | None:
        with self._lock:
            row = self.db.execute(
                "SELECT sid, rel_path, abs_path, size, mtime, sha256, role, state, extracted_handle FROM sources WHERE sid=?",
                (sid,),
            ).fetchone()
        return SourceRecord(*row) if row else None

    def extract_source(
        self,
        sid: str,
        store: ArtifactStore,
        session: str = "default",
    ) -> dict[str, Any]:
        """On-demand extraction with artifact caching."""
        rec = self.get_source(sid)
        if not rec:
            raise KeyError(f"source {sid!r} not found in ledger")

        # Check if already extracted and artifact exists
        if rec.extracted_handle and store.exists(rec.extracted_handle):
            raw = store.read_bytes(rec.extracted_handle)
            return {
                "sid": sid,
                "handle": rec.extracted_handle,
                "state": rec.state,
                "text_preview": raw[:2000].decode("utf-8", errors="replace"),
                "total_bytes": len(raw),
                "cached": True,
            }

        # Perform extraction
        res = extract_document(rec.abs_path)
        handle = store.put_bytes(
            res.text.encode("utf-8"),
            kind="doc",
            session=session,
            taint=taint.EXTERNAL,
        )

        with self._lock:
            self.db.execute(
                "UPDATE sources SET state=?, extracted_handle=? WHERE sid=?",
                (res.state, handle, sid),
            )

        return {
            "sid": sid,
            "handle": handle,
            "state": res.state,
            "reason": res.reason,
            "text_preview": res.text[:2000],
            "pages": res.pages,
            "chunks_count": len(res.chunks),
            "cached": False,
        }
