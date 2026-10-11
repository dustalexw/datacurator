"""Operation plans stored as SQLite files so very large plans stay off the heap.

The UI reads pages of items with filters; the executor streams items in id order.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterator

from ..models import Action, Flag, PlanItem

FILTERS = {
    "all": "1=1",
    "changes": "action IN ('move','rewrite','write_tags','quarantine')",
    "unchanged": "action='none'",
    "review": "action='review'",
    "conflicts": "flags LIKE '%conflict%'",
    "uncertain": "flags LIKE '%uncertain%'",
    "duplicates": "flags LIKE '%duplicate%'",
    "errors": "flags LIKE '%error%'",
    "sanitized": "flags LIKE '%sanitized%'",
    "limitations": "flags LIKE '%limitation%'",
    "skipped": "action='skip'",
}


class PlanStore:
    def __init__(self, path: str, create: bool = False):
        self.path = path
        if create and os.path.exists(path):
            raise FileExistsError(path)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS header(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY, source TEXT, dest TEXT, dest_key TEXT,
          dest_dir TEXT, category TEXT, action TEXT, role TEXT, parent_id INTEGER, grp TEXT, flags TEXT,
          conflict TEXT, size INTEGER, reason TEXT, data TEXT);
        CREATE INDEX IF NOT EXISTS items_dest_key ON items(dest_key);
        CREATE INDEX IF NOT EXISTS items_source ON items(source);
        CREATE INDEX IF NOT EXISTS items_action ON items(action);
        CREATE INDEX IF NOT EXISTS items_parent ON items(parent_id);
        CREATE INDEX IF NOT EXISTS items_grp ON items(grp);
        CREATE INDEX IF NOT EXISTS items_dest_dir ON items(dest_dir);
        """)
        self._pending: list[PlanItem] = []

    @classmethod
    def new(cls, directory: str) -> "PlanStore":
        os.makedirs(directory, exist_ok=True)
        pid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        ps = cls(os.path.join(directory, f"plan-{pid}.sqlite"), create=True)
        ps.set_header("plan_id", pid)
        ps.set_header("created", time.time())
        return ps

    @property
    def plan_id(self) -> str:
        return self.header("plan_id") or os.path.basename(self.path)

    def close(self) -> None:
        self.flush()
        self.db.close()

    # header
    def set_header(self, key: str, value: Any) -> None:
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO header VALUES(?,?)", (key, json.dumps(value)))

    def header(self, key: str, default: Any = None) -> Any:
        with self._lock:
            r = self.db.execute("SELECT value FROM header WHERE key=?", (key,)).fetchone()
        return json.loads(r[0]) if r else default

    # items
    @staticmethod
    def _row(item: PlanItem) -> tuple:
        return (item.id, item.source, item.dest, item.dest_key,
                os.path.dirname(item.dest) if item.dest else None, item.category, item.action, item.role,
                item.parent_id, item.group, ",".join(sorted(set(item.flags))), item.conflict, item.size,
                item.reason, json.dumps(item.to_dict(), ensure_ascii=False))

    def add(self, item: PlanItem) -> None:
        self._pending.append(item)
        if len(self._pending) >= 2000:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        with self._lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                [self._row(i) for i in self._pending])
            self.db.execute("COMMIT")
        self._pending.clear()

    def update(self, item: PlanItem) -> None:
        self.flush()
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", self._row(item))

    def update_many(self, items: list[PlanItem]) -> None:
        self.flush()
        with self._lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                [self._row(i) for i in items])
            self.db.execute("COMMIT")

    def get(self, item_id: int) -> PlanItem | None:
        self.flush()
        with self._lock:
            r = self.db.execute("SELECT data FROM items WHERE id=?", (item_id,)).fetchone()
        return PlanItem.from_dict(json.loads(r[0])) if r else None

    def by_source(self, source: str) -> PlanItem | None:
        self.flush()
        with self._lock:
            r = self.db.execute("SELECT data FROM items WHERE source=? ORDER BY id LIMIT 1", (source,)).fetchone()
        return PlanItem.from_dict(json.loads(r[0])) if r else None

    def query(self, where: str = "1=1", args: tuple = (), order: str = "id") -> Iterator[PlanItem]:
        self.flush()
        last = -1
        while True:
            with self._lock:
                rows = self.db.execute(f"SELECT id, data FROM items WHERE ({where}) AND id>? ORDER BY id LIMIT 1000",
                                       (*args, last)).fetchall()
            if not rows:
                return
            for r in rows:
                yield PlanItem.from_dict(json.loads(r["data"]))
            last = rows[-1]["id"]

    def items(self) -> Iterator[PlanItem]:
        return self.query()

    def children(self, parent_id: int) -> list[PlanItem]:
        return list(self.query("parent_id=?", (parent_id,)))

    def scalar(self, sql: str, args: tuple = ()) -> Any:
        self.flush()
        with self._lock:
            r = self.db.execute(sql, args).fetchone()
        return r[0] if r else None

    def rows(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        self.flush()
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def page(self, filt: str = "all", text: str = "", offset: int = 0, limit: int = 200,
             category: str | None = None) -> list[dict[str, Any]]:
        where, args = self._where(filt, text, category)
        with self._lock:
            rows = self.db.execute(
                f"SELECT id, source, dest, category, action, flags, reason, size, role FROM items WHERE {where} "
                f"ORDER BY COALESCE(dest, source), id LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
        return [dict(r) for r in rows]

    def count(self, filt: str = "all", text: str = "", category: str | None = None) -> int:
        where, args = self._where(filt, text, category)
        return self.scalar(f"SELECT COUNT(*) FROM items WHERE {where}", args)

    def _where(self, filt: str, text: str, category: str | None) -> tuple[str, tuple]:
        self.flush()
        clauses = [FILTERS.get(filt, "1=1")]
        args: list[Any] = []
        if text:
            clauses.append("(source LIKE ? OR dest LIKE ? OR reason LIKE ?)")
            args += [f"%{text}%"] * 3
        if category:
            clauses.append("category=?")
            args.append(category)
        return " AND ".join(f"({c})" for c in clauses), tuple(args)

    def summary(self) -> dict[str, Any]:
        self.flush()
        s: dict[str, Any] = {}
        for k in FILTERS:
            s[k] = self.count(k)
        s["bytes_to_copy"] = self.scalar(
            "SELECT COALESCE(SUM(size),0) FROM items WHERE action='move' AND data LIKE '%\"cross_device\": true%'")
        s["folders_to_create"] = len(self.header("new_dirs", []) or [])
        return s

    # reports
    def export_csv(self, path: str) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["id", "action", "category", "source", "destination", "flags", "reason", "conflict",
                        "limitations", "sanitized", "size", "cross_device"])
            for it in self.items():
                w.writerow([it.id, it.action, it.category, it.source, it.dest or "", ";".join(it.flags),
                            it.reason, it.conflict or "", " | ".join(it.limitations),
                            " | ".join(f"{c['before']} → {c['after']} ({'; '.join(c['reasons'])})"
                                       for c in it.sanitized), it.size, it.cross_device])

    def export_json(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"header": ')
            hdr = {r["key"]: json.loads(r["value"]) for r in self.rows("SELECT key, value FROM header")}
            json.dump(hdr, fh, ensure_ascii=False, indent=1)
            fh.write(', "items": [\n')
            first = True
            for it in self.items():
                if not first:
                    fh.write(",\n")
                json.dump(it.to_dict(), fh, ensure_ascii=False)
                first = False
            fh.write("\n]}\n")


def explain(item: PlanItem) -> str:
    """Plain-language description used by CLI and GUI detail panes."""
    lines = []
    src_dir, src_name = os.path.split(item.source)
    if item.action == Action.MOVE.value and item.dest:
        dst_dir, dst_name = os.path.split(item.dest)
        if src_dir == dst_dir:
            lines.append(f"Rename “{src_name}” to “{dst_name}” in the same folder.")
        elif src_name == dst_name:
            lines.append(f"Move “{src_name}” into {dst_dir}.")
        else:
            lines.append(f"Move “{src_name}” to {dst_dir} and rename it “{dst_name}”.")
        lines.append(("This copies the file to another drive, checks the copy, then removes the original."
                      if item.cross_device else "This is a rename on the same drive; file contents are not touched."))
    elif item.action == Action.NONE.value:
        lines.append("No change: the file already matches the rules.")
    elif item.action == Action.REVIEW.value:
        lines.append("Left in place for review.")
    elif item.action == Action.SKIP.value:
        lines.append("Left in place.")
    elif item.action == Action.REWRITE.value:
        lines.append("Update file references inside this companion file (original kept in the recovery journal).")
    elif item.action == Action.WRITE_TAGS.value:
        lines.append("Write embedded tags (original file kept in the recovery journal).")
    elif item.action == Action.QUARANTINE.value:
        lines.append("Move this exact duplicate into the quarantine folder (undoable).")
    if item.reason:
        lines.append("Why: " + item.reason)
    if item.conflict:
        lines.append("Conflict: " + item.conflict)
    for c in item.sanitized:
        lines.append(f"Name adjusted: “{c['before']}” → “{c['after']}” ({'; '.join(c['reasons'])}).")
    for lim in item.limitations:
        lines.append("Preservation: " + lim)
    prov = item.provenance or {}
    if prov:
        srcs = ", ".join(f"{k} from {v}" for k, v in sorted(prov.items()))
        lines.append("Metadata used: " + srcs + ".")
    if item.suggestions:
        lines.append("Suggested fix: " + ", ".join(f"{k} = {v}" for k, v in sorted(item.suggestions.items()) if not k.startswith("_")))
    if Flag.DUPLICATE.value in item.flags and item.content_hash:
        lines.append(f"Content fingerprint: {item.content_hash[:16]}…")
    return "\n".join(lines)
