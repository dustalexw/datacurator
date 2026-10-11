"""Durable recovery journal (SQLite, synchronous=FULL).

Every state transition is committed before the corresponding filesystem step,
so after a crash or power loss the journal tells recovery exactly which step
may have been in progress.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

TERMINAL = {"done", "failed", "skipped", "cancelled", "reverted", "conflict"}


@dataclass
class Op:
    seq: int
    batch: str
    item_id: int | None
    kind: str          # move | stage | mkdir | rmdir | rewrite | write_tags | quarantine
    src: str
    dst: str
    tmp: str | None = None
    state: str = "pending"
    cross: bool = False
    src_sig: dict[str, Any] = field(default_factory=dict)
    dst_sig: dict[str, Any] = field(default_factory=dict)
    src_hash: str | None = None
    backup: str | None = None
    after_hash: str | None = None
    error: str | None = None
    note: str | None = None
    root: str | None = None


class Journal:
    def __init__(self, path: str, backup_dir: str):
        self.path = path
        self.backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY, kind TEXT, plan_path TEXT, created REAL,
          finished REAL, status TEXT, message TEXT, roots TEXT, options TEXT, summary TEXT, parent TEXT);
        CREATE TABLE IF NOT EXISTS ops(seq INTEGER PRIMARY KEY AUTOINCREMENT, batch TEXT, item_id INTEGER,
          kind TEXT, src TEXT, dst TEXT, tmp TEXT, state TEXT, cross INTEGER, src_sig TEXT, dst_sig TEXT,
          src_hash TEXT, backup TEXT, after_hash TEXT, error TEXT, note TEXT, root TEXT, updated REAL);
        CREATE INDEX IF NOT EXISTS ops_batch ON ops(batch, seq);
        """)

    def close(self) -> None:
        self.db.close()

    # batches
    def new_batch(self, kind: str, plan_path: str | None, roots: dict, options: dict,
                  parent: str | None = None) -> str:
        bid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        with self._lock:
            self.db.execute("INSERT INTO batches VALUES(?,?,?,?,NULL,'running',NULL,?,?,NULL,?)",
                            (bid, kind, plan_path, time.time(), json.dumps(roots), json.dumps(options), parent))
        return bid

    def set_batch(self, bid: str, status: str, message: str | None = None, summary: dict | None = None,
                  finished: bool = False) -> None:
        with self._lock:
            self.db.execute("UPDATE batches SET status=?, message=COALESCE(?, message), "
                            "summary=COALESCE(?, summary), finished=CASE WHEN ? THEN ? ELSE finished END WHERE id=?",
                            (status, message, json.dumps(summary) if summary is not None else None,
                             int(finished), time.time(), bid))

    def batch(self, bid: str) -> dict[str, Any] | None:
        with self._lock:
            r = self.db.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        for k in ("roots", "options", "summary"):
            d[k] = json.loads(d[k]) if d[k] else {}
        return d

    def batches(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            ids = [r[0] for r in self.db.execute("SELECT id FROM batches ORDER BY created DESC LIMIT ?", (limit,))]
        return [self.batch(i) for i in ids]

    def unfinished(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = [r[0] for r in self.db.execute(
                "SELECT id FROM batches WHERE status IN ('running','paused','interrupted') ORDER BY created")]
        return [self.batch(i) for i in ids]

    # ops
    def add(self, op: Op) -> Op:
        with self._lock:
            cur = self.db.execute(
                "INSERT INTO ops(batch,item_id,kind,src,dst,tmp,state,cross,src_sig,dst_sig,src_hash,backup,"
                "after_hash,error,note,root,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (op.batch, op.item_id, op.kind, op.src, op.dst, op.tmp, op.state, int(op.cross),
                 json.dumps(op.src_sig), json.dumps(op.dst_sig), op.src_hash, op.backup, op.after_hash,
                 op.error, op.note, op.root, time.time()))
            op.seq = cur.lastrowid
        return op

    def save(self, op: Op) -> None:
        with self._lock:
            self.db.execute(
                "UPDATE ops SET src=?, dst=?, tmp=?, state=?, cross=?, src_sig=?, dst_sig=?, src_hash=?, backup=?, "
                "after_hash=?, error=?, note=?, updated=? WHERE seq=?",
                (op.src, op.dst, op.tmp, op.state, int(op.cross), json.dumps(op.src_sig), json.dumps(op.dst_sig),
                 op.src_hash, op.backup, op.after_hash, op.error, op.note, time.time(), op.seq))

    def set_state(self, op: Op, state: str, **kw) -> None:
        op.state = state
        for k, v in kw.items():
            setattr(op, k, v)
        self.save(op)

    @staticmethod
    def _op(r: sqlite3.Row) -> Op:
        return Op(seq=r["seq"], batch=r["batch"], item_id=r["item_id"], kind=r["kind"], src=r["src"], dst=r["dst"],
                  tmp=r["tmp"], state=r["state"], cross=bool(r["cross"]), src_sig=json.loads(r["src_sig"] or "{}"),
                  dst_sig=json.loads(r["dst_sig"] or "{}"), src_hash=r["src_hash"], backup=r["backup"],
                  after_hash=r["after_hash"], error=r["error"], note=r["note"], root=r["root"])

    def ops(self, bid: str, states: set[str] | None = None, reverse: bool = False) -> list[Op]:
        with self._lock:
            rows = self.db.execute(f"SELECT * FROM ops WHERE batch=? ORDER BY seq {'DESC' if reverse else 'ASC'}",
                                   (bid,)).fetchall()
        out = [self._op(r) for r in rows]
        return [o for o in out if states is None or o.state in states]

    def done_items(self, bid: str) -> set[int]:
        with self._lock:
            return {r[0] for r in self.db.execute(
                "SELECT item_id FROM ops WHERE batch=? AND state='done' "
                "AND item_id IS NOT NULL AND kind IN ('move','quarantine','rewrite','write_tags')", (bid,))}

    def backup_path(self, bid: str, name: str) -> str:
        d = os.path.join(self.backup_dir, bid)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name)
