"""Local SQLite catalog: scanned files, metadata cache, overrides, exclusions,
tags, collections, saved locations and capability probes.

Search, tags, collections and the review queue live here — never as folders.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

from ..models import FileRecord

SCHEMA_VERSION = 1
META_VERSION = 4  # re-read cached HEIC/HEIF failures with the new metadata reader

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta_info(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY, root TEXT NOT NULL, dir TEXT NOT NULL, name TEXT NOT NULL,
  size INTEGER, mtime_ns INTEGER, ino INTEGER, dev INTEGER, nlink INTEGER,
  kind TEXT, meta TEXT, meta_error TEXT, meta_version INTEGER,
  hash TEXT, hash_sig TEXT, via_symlink INTEGER DEFAULT 0, seen_scan INTEGER);
CREATE INDEX IF NOT EXISTS files_dir ON files(dir);
CREATE INDEX IF NOT EXISTS files_root ON files(root);
CREATE INDEX IF NOT EXISTS files_size ON files(size);
CREATE TABLE IF NOT EXISTS overrides(target TEXT, field TEXT, value TEXT, PRIMARY KEY(target, field));
CREATE TABLE IF NOT EXISTS exclusions(path TEXT PRIMARY KEY, reason TEXT);
CREATE TABLE IF NOT EXISTS tags(path TEXT, tag TEXT, PRIMARY KEY(path, tag));
CREATE TABLE IF NOT EXISTS collections(name TEXT, path TEXT, PRIMARY KEY(name, path));
CREATE TABLE IF NOT EXISTS locations(name TEXT PRIMARY KEY, path TEXT, kind TEXT, protocol TEXT,
  server TEXT, share TEXT, username TEXT, identity TEXT, added REAL);
CREATE TABLE IF NOT EXISTS capabilities(path TEXT PRIMARY KEY, data TEXT, probed_at REAL);
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT, started REAL, finished REAL,
  roots TEXT, stats TEXT, completed INTEGER DEFAULT 0);
"""


def default_data_dir() -> str:
    env = os.environ.get("DATACURATOR_HOME")
    if env:
        return env
    try:
        from platformdirs import user_data_dir
        return user_data_dir("DataCurator", "DataCurator")
    except Exception:  # pragma: no cover
        return os.path.join(os.path.expanduser("~"), ".datacurator")


class Catalog:
    def __init__(self, path: str | None = None):
        if path is None:
            d = default_data_dir()
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "catalog.sqlite")
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT OR IGNORE INTO meta_info VALUES('schema', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.db.execute("BEGIN")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ files
    def dir_index(self, d: str) -> dict[str, sqlite3.Row]:
        with self._lock:
            rows = self.db.execute(
                "SELECT name, size, mtime_ns, ino, meta_version, meta, meta_error, hash, hash_sig "
                "FROM files WHERE dir=?", (d,)).fetchall()
        return {r["name"]: r for r in rows}

    def upsert_files(self, recs: Iterable[tuple[FileRecord, int, bool]]) -> None:
        """recs: (record, scan_id, meta_changed)."""
        with self.tx() as db:
            for rec, scan_id, meta_changed in recs:
                d, n = os.path.split(rec.path)
                if meta_changed:
                    db.execute(
                        "INSERT INTO files(path, root, dir, name, size, mtime_ns, ino, dev, nlink, kind, meta, "
                        "meta_error, meta_version, hash, hash_sig, via_symlink, seen_scan) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?) "
                        "ON CONFLICT(path) DO UPDATE SET root=excluded.root, size=excluded.size, "
                        "mtime_ns=excluded.mtime_ns, ino=excluded.ino, dev=excluded.dev, nlink=excluded.nlink, "
                        "kind=excluded.kind, meta=excluded.meta, meta_error=excluded.meta_error, "
                        "meta_version=excluded.meta_version, hash=NULL, hash_sig=NULL, "
                        "via_symlink=excluded.via_symlink, seen_scan=excluded.seen_scan",
                        (rec.path, rec.root, d, n, rec.size, rec.mtime_ns, rec.ino, rec.dev, rec.nlink,
                         rec.kind, json.dumps(rec.meta, sort_keys=True), rec.meta_error, META_VERSION,
                         int(rec.via_symlink), scan_id))
                else:
                    db.execute("UPDATE files SET seen_scan=?, root=?, dev=?, nlink=? WHERE path=?",
                               (scan_id, rec.root, rec.dev, rec.nlink, rec.path))

    def prune_unseen(self, root: str, scan_id: int) -> int:
        with self.tx() as db:
            prefix = root.rstrip(os.sep) + os.sep
            cur = db.execute("DELETE FROM files WHERE (root=? OR path LIKE ? ESCAPE '\\') AND "
                             "(seen_scan IS NULL OR seen_scan<>?)",
                             (root, _like_prefix(prefix), scan_id))
            return cur.rowcount

    def _row_to_record(self, r: sqlite3.Row) -> FileRecord:
        return FileRecord(path=r["path"], root=r["root"], size=r["size"], mtime_ns=r["mtime_ns"],
                          ino=r["ino"], dev=r["dev"], nlink=r["nlink"] or 1, kind=r["kind"],
                          meta=json.loads(r["meta"] or "{}"), meta_error=r["meta_error"],
                          via_symlink=bool(r["via_symlink"]))

    def iter_dirs(self, roots: list[str]) -> Iterator[tuple[str, list[FileRecord]]]:
        """Yield (directory, records) grouped by directory, deterministic order, bounded memory."""
        for root in sorted(roots):
            prefix = root.rstrip(os.sep) + os.sep
            with self._lock:
                dirs = [r[0] for r in self.db.execute(
                    "SELECT DISTINCT dir FROM files WHERE root=? OR path LIKE ? ESCAPE '\\' ORDER BY dir",
                    (root, _like_prefix(prefix)))]
            for d in dirs:
                with self._lock:
                    rows = self.db.execute("SELECT * FROM files WHERE dir=? ORDER BY name", (d,)).fetchall()
                yield d, [self._row_to_record(r) for r in rows]

    def get(self, path: str) -> FileRecord | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone()
        return self._row_to_record(r) if r else None

    def count(self, roots: list[str] | None = None) -> int:
        with self._lock:
            if not roots:
                return self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            n = 0
            for root in roots:
                prefix = root.rstrip(os.sep) + os.sep
                n += self.db.execute("SELECT COUNT(*) FROM files WHERE root=? OR path LIKE ? ESCAPE '\\'",
                                     (root, _like_prefix(prefix))).fetchone()[0]
            return n

    def size_peers(self, size: int) -> list[str]:
        with self._lock:
            return [r[0] for r in self.db.execute("SELECT path FROM files WHERE size=?", (size,))]

    def cached_hash(self, path: str, sig: str) -> str | None:
        with self._lock:
            r = self.db.execute("SELECT hash FROM files WHERE path=? AND hash_sig=?", (path, sig)).fetchone()
        return r[0] if r else None

    def store_hash(self, path: str, sig: str, h: str) -> None:
        with self._lock:
            self.db.execute("UPDATE files SET hash=?, hash_sig=? WHERE path=?", (h, sig, path))

    def search(self, text: str = "", tag: str | None = None, collection: str | None = None,
               limit: int = 200) -> list[str]:
        q = "SELECT f.path FROM files f"
        args: list[Any] = []
        where = []
        if tag:
            q += " JOIN tags t ON t.path=f.path"
            where.append("t.tag=?")
            args.append(tag)
        if collection:
            q += " JOIN collections c ON c.path=f.path"
            where.append("c.name=?")
            args.append(collection)
        if text:
            where.append("(f.path LIKE ? OR f.meta LIKE ?)")
            args += [f"%{text}%", f"%{text}%"]
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY f.path LIMIT ?"
        args.append(limit)
        with self._lock:
            return [r[0] for r in self.db.execute(q, args)]

    def rename_paths(self, mapping: list[tuple[str, str]]) -> None:
        """Keep catalog paths (and their overrides/tags) pointing at files after they move."""
        with self.tx() as db:
            for old, new in mapping:
                d, n = os.path.split(new)
                db.execute("DELETE FROM files WHERE path=?", (new,))
                db.execute("UPDATE files SET path=?, dir=?, name=? WHERE path=?", (new, d, n, old))
                for table, col in (("overrides", "target"), ("exclusions", "path"), ("tags", "path"),
                                   ("collections", "path")):
                    db.execute(f"UPDATE OR IGNORE {table} SET {col}=? WHERE {col}=?", (new, old))

    # ------------------------------------------------------------- overrides
    def set_override(self, target: str, field: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self.db.execute("DELETE FROM overrides WHERE target=? AND field=?", (target, field))
            else:
                self.db.execute("INSERT OR REPLACE INTO overrides VALUES(?,?,?)", (target, field, value))

    def overrides(self) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}
        with self._lock:
            for r in self.db.execute("SELECT target, field, value FROM overrides ORDER BY target, field"):
                out.setdefault(r[0], {})[r[1]] = r[2]
        return out

    def set_excluded(self, path: str, excluded: bool, reason: str = "user") -> None:
        with self._lock:
            if excluded:
                self.db.execute("INSERT OR REPLACE INTO exclusions VALUES(?,?)", (path, reason))
            else:
                self.db.execute("DELETE FROM exclusions WHERE path=?", (path,))

    def exclusions(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self.db.execute("SELECT path FROM exclusions")}

    # ------------------------------------------------------- tags/collections
    def add_tag(self, path: str, tag: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO tags VALUES(?,?)", (path, tag))

    def remove_tag(self, path: str, tag: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM tags WHERE path=? AND tag=?", (path, tag))

    def add_to_collection(self, name: str, path: str) -> None:
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO collections VALUES(?,?)", (name, path))

    def collections(self) -> list[tuple[str, int]]:
        with self._lock:
            return [(r[0], r[1]) for r in self.db.execute(
                "SELECT name, COUNT(*) FROM collections GROUP BY name ORDER BY name")]

    # -------------------------------------------------------------- locations
    def save_location(self, name: str, path: str, kind: str = "local", protocol: str = "",
                      server: str = "", share: str = "", username: str = "",
                      identity: dict | None = None) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO locations VALUES(?,?,?,?,?,?,?,?,?)",
                            (name, path, kind, protocol, server, share, username,
                             json.dumps(identity or {}), time.time()))

    def locations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute("SELECT * FROM locations ORDER BY name").fetchall()
        return [{**dict(r), "identity": json.loads(r["identity"] or "{}")} for r in rows]

    def remove_location(self, name: str) -> None:
        with self._lock:
            self.db.execute("DELETE FROM locations WHERE name=?", (name,))

    # ----------------------------------------------------------- capabilities
    def store_capabilities(self, path: str, data: dict) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO capabilities VALUES(?,?,?)",
                            (path, json.dumps(data), time.time()))

    def cached_capabilities(self, path: str) -> dict | None:
        with self._lock:
            r = self.db.execute("SELECT data FROM capabilities WHERE path=?", (path,)).fetchone()
        return json.loads(r[0]) if r else None

    # ------------------------------------------------------------------ scans
    def begin_scan(self, roots: list[str]) -> int:
        with self._lock:
            cur = self.db.execute("INSERT INTO scans(started, roots) VALUES(?,?)", (time.time(), json.dumps(roots)))
            return cur.lastrowid

    def end_scan(self, scan_id: int, stats: dict, completed: bool) -> None:
        with self._lock:
            self.db.execute("UPDATE scans SET finished=?, stats=?, completed=? WHERE id=?",
                            (time.time(), json.dumps(stats), int(completed), scan_id))


def _like_prefix(prefix: str) -> str:
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
