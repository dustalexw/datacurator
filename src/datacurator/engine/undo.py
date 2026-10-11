"""Undo a batch from its journal, checking file identity before every step.

A file is moved back only if it is still the file this application put there
(same inode/size/mtime for renames; same content hash for copies) and its
original location is free. Anything else is reported, never forced.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..storage.catalog import Catalog
from ..storage.fsops import LocalFS
from .executor import Control, Executor, _sig, copy_move, move_file
from .journal import Journal, Op
from .util import hash_file


@dataclass
class UndoReport:
    batch: str
    undo_batch: str = ""
    restored: int = 0
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.conflicts

    def summary(self) -> str:
        s = f"Restored {self.restored} item(s)."
        if self.conflicts:
            s += (f" {len(self.conflicts)} item(s) could not be restored automatically — complete restoration "
                  "is not possible without your decision:\n  - " + "\n  - ".join(self.conflicts))
        return s


class _UndoRunner(Executor):
    """Reuses the executor's journaled move primitives for the reverse direction."""

    def __init__(self, journal: Journal, fs: LocalFS, sleep):
        self.j = journal
        self.fs = fs
        self.control = Control()
        self.result = None
        self.retries = 3
        self.sleep = sleep
        self.resume_batch = None
        self.batch = ""

    def partial_for(self, op):
        return None


def _still_ours(fs: LocalFS, op: Op) -> tuple[bool, str]:
    path = op.dst
    if not fs.exists(path):
        return False, f"{path} no longer exists (moved or deleted after organizing)"
    st = fs.stat(path)
    sig = op.dst_sig or {}
    if sig:
        exact_time = st.st_mtime_ns == sig.get("mtime_ns")
        # network clients may round timestamps differently after a reconnect
        near_time = abs(st.st_mtime_ns - sig.get("mtime_ns", 0)) <= 2_000_000_000
        if st.st_size != sig.get("size") or not near_time:
            return False, f"{path} was modified after organizing; left as is"
        if not op.cross and sig.get("ino") and st.st_ino != sig.get("ino") and not exact_time:
            # inode changed (e.g. share reconnected) and timestamp not identical: verify nothing else
            return False, f"{path} is a different file than the one organized; left as is"
    if op.cross and op.src_hash and hash_file(path) != op.src_hash:
        return False, f"{path} content differs from what was copied; left as is"
    return True, ""


def undo_batch(journal: Journal, catalog: Catalog | None, batch_id: str, fs: LocalFS | None = None,
               sleep=time.sleep, dry_run: bool = False) -> UndoReport:
    fs = fs or LocalFS()
    b = journal.batch(batch_id)
    if b is None:
        raise ValueError(f"No batch {batch_id}")
    rep = UndoReport(batch_id)
    if b["status"] in ("running",):
        raise ValueError("This batch is still running or was interrupted; run recovery first.")
    ops = journal.ops(batch_id, states={"done"}, reverse=True)
    # folders this batch created are removed last, deepest first, and only if empty
    ops = [o for o in ops if o.kind != "mkdir"] + sorted(
        (o for o in ops if o.kind == "mkdir"), key=lambda o: -o.dst.count(os.sep))
    runner = _UndoRunner(journal, fs, sleep)
    if not dry_run:
        runner.batch = journal.new_batch("undo", None, b.get("roots") or {}, {}, parent=batch_id)
        rep.undo_batch = runner.batch
    moved_back: list[tuple[str, str]] = []
    for op in ops:
        if op.kind in ("move", "quarantine", "stage"):
            ok, why = _still_ours(fs, op)
            if not ok:
                rep.conflicts.append(why)
                continue
            if fs.exists(op.src):
                rep.conflicts.append(f"{op.src} is occupied by another file now; {op.dst} was left in place")
                continue
            if dry_run:
                rep.restored += 1
                continue
            parent = os.path.dirname(op.src)
            if not fs.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                    rep.notes.append(f"Recreated folder {parent}")
                except OSError as e:
                    rep.conflicts.append(f"could not recreate {parent}: {e.strerror}")
                    continue
            back = journal.add(Op(0, runner.batch, op.item_id, "move", op.dst, op.src, state="pending",
                                  cross=op.cross, src_sig=dict(op.dst_sig), note=f"undo of #{op.seq}"))
            try:
                move_file(runner, back, op.dst, op.src)
                journal.set_state(op, "reverted", note=f"undone by {runner.batch}")
                rep.restored += 1
                if op.kind != "stage":
                    moved_back.append((op.dst, op.src))
            except FileExistsError:
                journal.set_state(back, "conflict")
                rep.conflicts.append(f"{op.src} appeared during undo; nothing overwritten")
            except Exception as e:  # keep going; report precisely
                journal.set_state(back, "failed", error=str(e))
                rep.conflicts.append(f"could not move {op.dst} back: {e}")
        elif op.kind in ("rewrite", "write_tags"):
            if not fs.exists(op.dst):
                rep.conflicts.append(f"{op.dst} no longer exists; backup kept at {op.backup}")
                continue
            if hash_file(op.dst) != op.after_hash:
                rep.conflicts.append(f"{op.dst} changed after it was edited; original kept at {op.backup}")
                continue
            if dry_run:
                rep.restored += 1
                continue
            tmp = op.dst + ".curator-restore"
            import shutil
            shutil.copy2(op.backup, tmp)
            fs.replace_own(tmp, op.dst)
            journal.set_state(op, "reverted", note=f"undone by {runner.batch}")
            rep.restored += 1
            # the move that brought this file here is undone next; it must recognise the restored file
            new_sig = _sig(fs.stat(op.dst))
            for mv in ops:
                if mv.kind in ("move", "quarantine") and mv.dst == op.dst and mv.state == "done":
                    mv.dst_sig = new_sig
                    if mv.cross:
                        mv.src_hash = hash_file(op.dst)
                    journal.save(mv)
        elif op.kind == "mkdir":
            if dry_run:
                continue
            try:
                if fs.isdir(op.dst) and not fs.listdir(op.dst):
                    fs.rmdir(op.dst)
                    journal.set_state(op, "reverted")
                elif fs.isdir(op.dst):
                    rep.notes.append(f"Kept folder {op.dst}: it is not empty")
            except OSError:
                rep.notes.append(f"Kept folder {op.dst}")
        elif op.kind == "rmdir":
            if dry_run:
                continue
            try:
                os.makedirs(op.dst, exist_ok=True)
                journal.set_state(op, "reverted")
            except OSError as e:
                rep.conflicts.append(f"could not recreate removed folder {op.dst}: {e.strerror}")
    if not dry_run:
        status = "undone" if rep.complete else "partially_undone"
        journal.set_batch(batch_id, status, rep.summary())
        journal.set_batch(runner.batch, "completed", rep.summary(), finished=True)
        if catalog is not None and moved_back:
            catalog.rename_paths(moved_back)
    return rep
