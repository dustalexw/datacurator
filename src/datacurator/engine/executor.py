"""Safe execution of an operation plan.

Guarantees
----------
* Never overwrites: renames use no-replace primitives; copies are finalized with a
  no-replace rename from a temporary name inside the destination folder.
* Sources are removed only after the copy's content hash was verified and the
  copy is in its final place, and only if the source is unchanged.
* Every step is journaled (fsync'd SQLite) *before* it happens; `recover()` can
  finish or roll back any step after a crash, power loss or disconnect.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..models import Action, PlanItem, PreflightError
from ..naming.sanitize import NamingRules, equivalence_key
from ..storage.capabilities import Capabilities, VolumeIdentity, check_reachable, probe, volume_identity
from ..storage.catalog import Catalog
from ..storage.fsops import LocalFS
from .journal import Journal, Op
from .planstore import PlanStore
from .util import CHUNK, hash_file, is_within, is_within_real, same_entry

TRANSIENT_ERRNOS = {errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.EINTR, errno.ECONNRESET,
                    errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENETRESET, errno.ENETUNREACH,
                    errno.EIO, errno.ENOTCONN, getattr(errno, "ESTALE", 116), getattr(errno, "EHOSTDOWN", 112),
                    getattr(errno, "ECOMM", 70)}
TRANSIENT_WINERRORS = {32, 33, 53, 54, 59, 64, 67, 121, 1231}  # sharing/lock violation, network errors
UNAVAILABLE_ERRNOS = {errno.ENOTCONN, getattr(errno, "ESTALE", 116), getattr(errno, "EHOSTDOWN", 112),
                      errno.ENETUNREACH, errno.EHOSTUNREACH}


class Cancelled(Exception):
    pass


class StorageUnavailable(Exception):
    pass


class DiskFull(Exception):
    pass


class SourceChanged(Exception):
    pass


class VerifyFailed(Exception):
    pass


class Control:
    """Pause / cancel from another thread (GUI) or signal handler (CLI)."""

    def __init__(self):
        self.cancel_event = threading.Event()
        self.resume_event = threading.Event()
        self.resume_event.set()

    def pause(self):
        self.resume_event.clear()

    def resume(self):
        self.resume_event.set()

    def cancel(self):
        self.cancel_event.set()
        self.resume_event.set()

    @property
    def paused(self) -> bool:
        return not self.resume_event.is_set()

    def checkpoint(self):
        while not self.resume_event.wait(0.1):
            if self.cancel_event.is_set():
                break
        if self.cancel_event.is_set():
            raise Cancelled()


@dataclass
class ApplyResult:
    batch_id: str
    status: str
    done: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    message: str = ""
    preflight: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class PreflightReport:
    blocked: dict[int, str] = field(default_factory=dict)
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    caps: dict[str, Capabilities] = field(default_factory=dict)


def is_transient(e: OSError) -> bool:
    if getattr(e, "winerror", None) in TRANSIENT_WINERRORS:
        return True
    return e.errno in TRANSIENT_ERRNOS


class Executor:
    def __init__(self, plan: PlanStore, journal: Journal, catalog: Catalog | None = None,
                 progress: Callable[[dict], None] | None = None, control: Control | None = None,
                 fs: LocalFS | None = None, approve_deletes: bool = False,
                 cleanup_empty_dirs: bool | None = None, sleep: Callable[[float], None] = time.sleep,
                 retries: int | None = None, active_probe: bool = True, resume_batch: str | None = None,
                 item_ids: set[int] | None = None):
        self.plan = plan
        self.j = journal
        self.catalog = catalog
        self.progress = progress
        self.control = control or Control()
        self.fs = fs or LocalFS()
        self.approve_deletes = approve_deletes
        self.profile = plan.header("profile") or {}
        self.cleanup = self.profile.get("cleanup_empty_dirs", False) if cleanup_empty_dirs is None else cleanup_empty_dirs
        self.sleep = sleep
        net = self.profile.get("network") or {}
        self.retries = net.get("retries", 4) if retries is None else retries
        self.active_probe = active_probe
        self.resume_batch = resume_batch
        self.item_ids = item_ids
        self.rules = NamingRules.from_dict(self.profile.get("naming"))
        self.batch = ""
        self.result: ApplyResult | None = None
        self._failed_items: set[int] = set()
        self._roots: dict[str, VolumeIdentity] = {}
        self._moved: list[tuple[str, str]] = []
        self._src_dirs: set[str] = set()
        self._last_check: dict[str, float] = {}

    def partial_for(self, op: Op) -> str | None:
        """A partial copy kept from an interrupted run of the same item (resumable transfer)."""
        if not self.resume_batch:
            return None
        for prev in self.j.ops(self.batch, reverse=True):
            if prev.item_id == op.item_id and prev.seq != op.seq and prev.tmp and prev.state == "reverted" \
                    and prev.note and "partial kept" in prev.note and self.fs.exists(prev.tmp):
                return prev.tmp
        return None

    # ---------------------------------------------------------------- preflight
    def _items(self) -> list[PlanItem]:
        out = [it for it in self.plan.query(
            "action IN ('move','quarantine','rewrite','write_tags')")]
        if self.item_ids is not None:
            out = [i for i in out if i.id in self.item_ids]
        return out

    def preflight(self) -> PreflightReport:
        rep = PreflightReport()
        items = self._items()
        if any(i.action == Action.QUARANTINE.value for i in items) and not self.approve_deletes:
            rep.fatal.append("This plan removes duplicates. Approve duplicate removal explicitly to apply it.")
        dest_caps = {r: Capabilities.from_dict(c) for r, c in (self.plan.header("dest_caps") or {}).items()}
        sources = self.plan.header("sources") or {}
        # source roots: same storage as scanned?
        for root, ident in sources.items():
            if ident is None:
                continue
            ok, msg = check_reachable(root)
            if not ok:
                rep.fatal.append(f"Source {root} is not reachable: {msg}. {reconnect_hint(root)}")
                continue
            ok, why = VolumeIdentity.from_dict(ident).matches(volume_identity(root))
            if not ok:
                rep.fatal.append(f"Source {root}: {why}. Scan again before applying.")
            self._roots[root] = VolumeIdentity.from_dict(ident)
        used_roots = sorted({i.dest_root for i in items if i.dest_root})
        for root in used_roots:
            planned = dest_caps.get(root)
            base = root
            while not os.path.exists(base) and os.path.dirname(base) != base:
                base = os.path.dirname(base)
            ok, msg = check_reachable(base)
            if not ok:
                rep.fatal.append(f"Destination {root} is not reachable: {msg}. {reconnect_hint(root)}")
                continue
            now = probe(root) if self.active_probe else (planned or probe(root))
            rep.caps[root] = now
            if self.catalog is not None and now.probed == "active":
                self.catalog.store_capabilities(root, now.to_dict())
            if planned is not None:
                ref = planned.identity.path
                cur_ident = volume_identity(ref) if ref and os.path.isdir(ref) else now.identity
                ok, why = planned.identity.matches(cur_ident)
                if not ok:
                    rep.fatal.append(f"Destination {root}: {why}. Preview again before applying.")
                    continue
                if planned.naming_signature() != now.naming_signature():
                    rep.fatal.append(
                        f"Destination {root} behaves differently than assumed in the preview "
                        f"(measured: case-sensitive={now.case_sensitive}, Windows naming rules={now.windows_naming}). "
                        "Preview again; the new preview will use the measured behaviour.")
            if now.read_only:
                rep.fatal.append(f"Destination {root} is read-only.")
            self._roots.setdefault(root, planned.identity if planned is not None else now.identity)
        # space per destination device
        need: dict[str, int] = {}
        for it in items:
            if it.action in (Action.MOVE.value, Action.QUARANTINE.value) and it.cross_device and it.dest_root:
                need[it.dest_root] = need.get(it.dest_root, 0) + it.size
        for root, n in need.items():
            caps = rep.caps.get(root)
            free = caps.free_bytes if caps else None
            if free is not None and n + 64 * 1024 * 1024 > free:
                rep.fatal.append(f"Not enough free space on {root}: {fmt_bytes(n)} needed, {fmt_bytes(free)} free.")
        # per item checks
        for it in items:
            if it.action in (Action.REWRITE.value, Action.WRITE_TAGS.value):
                continue
            why = self._check_item(it, rep)
            if why:
                rep.blocked[it.id] = why
        return rep

    def _check_item(self, it: PlanItem, rep: PreflightReport) -> str | None:
        try:
            st = self.fs.stat(it.source)
        except FileNotFoundError:
            return "the file no longer exists"
        except OSError as e:
            return f"cannot read the file ({e.strerror})"
        sig = it.src_sig
        if sig and (st.st_size != sig.get("size") or st.st_mtime_ns != sig.get("mtime_ns") or
                    st.st_ino != sig.get("ino")):
            return "the file changed since it was scanned; scan again"
        if not it.dest or not it.dest_root:
            return "no destination"
        if not is_within(it.dest, it.dest_root) or not is_within_real(it.dest, it.dest_root):
            return "destination would be outside the selected destination folder"
        if self.fs.exists_entry(it.dest) and not same_entry(it.dest, it.source):
            occupant = self.plan.by_source(it.dest)
            if not (occupant and occupant.action == Action.MOVE.value):
                return f"a file already exists at {it.dest} (never overwritten)"
        src_dir = os.path.dirname(it.source)
        if not os.access(src_dir, os.W_OK):
            return "no permission to move files out of the source folder"
        base = os.path.dirname(it.dest)
        while not os.path.exists(base) and os.path.dirname(base) != base:
            base = os.path.dirname(base)
        if not os.access(base, os.W_OK | os.X_OK):
            return f"no permission to write in {base}"
        return None

    # ---------------------------------------------------------------- run
    def run(self) -> ApplyResult:
        rep = self.preflight()
        if rep.fatal:
            raise PreflightError("\n".join(rep.fatal))
        if self.resume_batch:
            bad = self._continuity_problems(self.resume_batch)
            if bad:
                raise PreflightError("The storage at the destination does not contain the files this run already "
                                     "wrote, so it may be a different drive or share:\n  " + "\n  ".join(bad[:5]))
            self.batch = self.resume_batch
            self.j.set_batch(self.batch, "running")
            already = self.j.done_items(self.batch)
        else:
            roots = {r: v.to_dict() for r, v in self._roots.items()}
            self.batch = self.j.new_batch("apply", self.plan.path, roots,
                                          {"cleanup_empty_dirs": self.cleanup, "plan_id": self.plan.plan_id})
            already = set()
        res = ApplyResult(self.batch, "running")
        res.preflight = [(self.plan.get(i).source, why) for i, why in sorted(rep.blocked.items())]
        self.result = res
        items = [i for i in self._items() if i.id not in already]
        for i in items:
            if i.id in rep.blocked:
                self._failed_items.add(i.id)
                res.skipped += 1
                self._journal_skip(i, rep.blocked[i.id])
        work = [i for i in items if i.id not in rep.blocked]
        moves = [i for i in work if i.action in (Action.MOVE.value, Action.QUARANTINE.value)]
        later = [i for i in work if i.action in (Action.REWRITE.value, Action.WRITE_TAGS.value)]
        total = len(work)
        try:
            staged = self._stage(moves)
            for n, it in enumerate(moves, 1):
                self._emit(n, total, it.source)
                self.control.checkpoint()
                if it.parent_id in self._failed_items:
                    self._failed_items.add(it.id)
                    res.skipped += 1
                    self._journal_skip(it, "kept with its main file, which was not moved")
                    continue
                self._run_item(it, staged.get(it.id))
            for n, it in enumerate(later, len(moves) + 1):
                self.control.checkpoint()
                if it.parent_id in self._failed_items:
                    res.skipped += 1
                    continue
                self._emit(n, total, it.source)
                self._run_content_op(it)
            if self.cleanup:
                self._cleanup_dirs()
            res.status = "completed" if not res.failed else "completed_with_errors"
            res.message = f"{res.done} done, {res.failed} failed, {res.skipped} skipped."
            self.j.set_batch(self.batch, res.status, res.message, self._summary(res), finished=True)
        except Cancelled:
            res.status = "cancelled"
            res.message = "Cancelled. Finished steps are kept and can be undone; nothing was left half-done."
            self.j.set_batch(self.batch, "cancelled", res.message, self._summary(res), finished=True)
        except StorageUnavailable as e:
            res.status = "interrupted"
            res.message = f"Storage became unavailable: {e}. Reconnect it and choose Resume. Nothing was lost."
            self.j.set_batch(self.batch, "interrupted", res.message, self._summary(res))
        except DiskFull as e:
            res.status = "failed"
            res.message = f"Destination is full ({e}). Completed steps are kept; remaining files were not touched."
            self.j.set_batch(self.batch, "failed", res.message, self._summary(res), finished=True)
        finally:
            if self.catalog is not None and self._moved:
                self.catalog.rename_paths(self._moved)
        return res

    def _continuity_problems(self, bid: str) -> list[str]:
        """Before resuming, prove the reconnected storage is the same: files this batch already
        placed must still be there with the recorded size and modification time."""
        problems = []
        done = [o for o in self.j.ops(bid, states={"done"}) if o.kind in ("move", "quarantine") and o.dst_sig]
        for o in done[-50:]:
            try:
                st = self.fs.stat(o.dst)
            except OSError:
                problems.append(f"missing: {o.dst}")
                continue
            if st.st_size != o.dst_sig.get("size") or abs(st.st_mtime_ns - o.dst_sig.get("mtime_ns", 0)) > 2e9:
                problems.append(f"different: {o.dst}")
        return problems

    def _summary(self, res: ApplyResult) -> dict:
        return {"done": res.done, "failed": res.failed, "skipped": res.skipped, "bytes_copied": res.bytes_copied,
                "errors": res.errors[:200]}

    def _emit(self, n: int, total: int, current: str, **kw):
        if self.progress:
            self.progress({"done": n - 1, "total": total, "current": current,
                           "bytes": self.result.bytes_copied if self.result else 0, **kw})

    def _journal_skip(self, it: PlanItem, why: str):
        self.j.add(Op(0, self.batch, it.id, "move", it.source, it.dest or "", state="skipped", error=why))
        if self.result:
            self.result.errors.append((it.source, why))

    # ---------------------------------------------------------------- staging (rename cycles, case-only)
    def _stage(self, moves: list[PlanItem]) -> dict[int, str]:
        """Move sources that another item wants to occupy (and case-only renames) to a
        temporary name in their own folder first. Returns item_id -> temporary path."""
        keyed: dict[str, PlanItem] = {}
        for it in moves:
            caps = None
            keyed[equivalence_key(it.source, caps, self.rules)] = it
        need: list[PlanItem] = []
        for it in moves:
            if it.payload.get("case_only"):
                need.append(it)
                continue
            dk = equivalence_key(it.dest, None, self.rules) if it.dest else None
            occ = keyed.get(dk) if dk else None
            if occ is not None and occ.id != it.id and occ not in need:
                need.append(occ)
        staged: dict[int, str] = {}
        for it in sorted(need, key=lambda i: i.id):
            self.control.checkpoint()
            d = os.path.dirname(it.source)
            tmp = os.path.join(d, f".curator-stage-{self.batch}-{it.id}")
            op = self.j.add(Op(0, self.batch, it.id, "stage", it.source, tmp, state="started",
                               src_sig=it.src_sig))
            try:
                self._verify_source(it)
                self._retry(self.fs.rename_noreplace, it.source, tmp, root=d)
                self.fs.fsync_dir(d)
                self.j.set_state(op, "done", dst_sig=_sig(self.fs.stat(tmp)))
                staged[it.id] = tmp
            except (SourceChanged, OSError) as e:
                self.j.set_state(op, "failed", error=str(e))
                self._fail(it, f"could not prepare rename: {e}")
        return staged

    # ---------------------------------------------------------------- items
    def _fail(self, it: PlanItem, why: str):
        self._failed_items.add(it.id)
        if self.result:
            self.result.failed += 1
            self.result.errors.append((it.source, why))

    def _verify_source(self, it: PlanItem, path: str | None = None):
        try:
            st = self.fs.stat(path or it.source)
        except FileNotFoundError:
            raise SourceChanged("the file disappeared")
        sig = it.src_sig
        if sig and (st.st_size != sig.get("size") or st.st_mtime_ns != sig.get("mtime_ns")
                    or st.st_ino != sig.get("ino")):
            raise SourceChanged("the file was modified after scanning")

    def _check_root(self, root: str | None, force: bool = False):
        if not root:
            return
        ident = self._roots.get(root)
        if ident is None:
            return
        now = time.monotonic()
        if not force and now - self._last_check.get(root, -1e9) < 2.0:
            return
        self._last_check[root] = now
        ref = ident.path or root
        ok, msg = check_reachable(ref, timeout=10)
        if not ok:
            raise StorageUnavailable(f"{root}: {msg}")
        if os.path.isdir(ref):
            ok, why = ident.matches(volume_identity(ref))
            if not ok:
                raise StorageUnavailable(f"{root}: {why} — refusing to continue on different storage")

    def _retry(self, fn, *args, root: str | None = None):
        delay = 0.5
        for attempt in range(self.retries + 1):
            try:
                return fn(*args)
            except FileExistsError:
                raise
            except OSError as e:
                if e.errno == errno.ENOSPC or getattr(e, "winerror", None) == 112:
                    raise DiskFull(e.strerror or "no space left")
                if not is_transient(e) or attempt == self.retries:
                    if e.errno in UNAVAILABLE_ERRNOS or getattr(e, "winerror", None) in (53, 64, 67):
                        raise StorageUnavailable(e.strerror or str(e))
                    if root and is_transient(e):
                        ok, msg = check_reachable(root, timeout=10)
                        if not ok:
                            raise StorageUnavailable(msg)
                    raise
                self.sleep(min(delay, 8.0))
                delay *= 2
                self.control.checkpoint()

    def _ensure_dirs(self, d: str, root: str, item_id: int):
        missing = []
        cur = d
        while not self.fs.isdir(cur):
            # inside the destination root, or the root's own missing parents (the folder the user chose)
            if not is_within(cur, root) and not is_within(root, cur):
                raise OSError(errno.EPERM, "refusing to create folders outside the destination", cur)
            missing.append(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        for p in reversed(missing):
            op = self.j.add(Op(0, self.batch, item_id, "mkdir", "", p, state="started"))
            try:
                self._retry(self.fs.mkdir, p, root=os.path.dirname(p))
            except FileExistsError:
                if not self.fs.isdir(p):
                    raise OSError(errno.ENOTDIR, "a file exists where a folder is needed", p)
                self.j.set_state(op, "skipped", note="already existed")
                continue
            self.fs.fsync_dir(os.path.dirname(p))
            self.j.set_state(op, "done")

    def _run_item(self, it: PlanItem, staged_from: str | None):
        res = self.result
        src = staged_from or it.source
        op = self.j.add(Op(0, self.batch, it.id, "quarantine" if it.action == Action.QUARANTINE.value else "move",
                           src, it.dest, state="pending", cross=it.cross_device, src_sig=dict(it.src_sig),
                           root=it.dest_root, note=("staged from " + it.source) if staged_from else None))
        try:
            self._check_root(it.dest_root)
            self._verify_source(it, src) if not staged_from else None
            if self.fs.exists_entry(it.dest):
                if not (self.fs.samefile(it.dest, src) or same_entry(it.dest, src)):
                    raise FileExistsError(errno.EEXIST, "destination exists", it.dest)
            self._ensure_dirs(os.path.dirname(it.dest), it.dest_root, it.id)
            move_file(self, op, src, it.dest)
            res.done += 1
            self._moved.append((it.source, it.dest))
            self._src_dirs.add(os.path.dirname(it.source))
        except FileExistsError:
            self.j.set_state(op, "conflict", error="destination appeared before the move; nothing overwritten")
            self._fail(it, "a file appeared at the destination; nothing was overwritten")
            if staged_from:
                self._unstage(it, staged_from)
        except SourceChanged as e:
            self.j.set_state(op, "skipped", error=str(e))
            self._fail(it, str(e))
        except (Cancelled, StorageUnavailable, DiskFull):
            raise
        except VerifyFailed as e:
            self.j.set_state(op, "failed", error=str(e))
            self._fail(it, str(e))
        except OSError as e:
            self.j.set_state(op, "failed", error=e.strerror or str(e))
            self._fail(it, f"{e.strerror or e} — original kept")
            if staged_from:
                self._unstage(it, staged_from)

    def _unstage(self, it: PlanItem, tmp: str):
        try:
            if self.fs.exists(tmp) and not self.fs.exists(it.source):
                op = self.j.add(Op(0, self.batch, it.id, "stage", tmp, it.source, state="started"))
                self.fs.rename_noreplace(tmp, it.source)
                self.j.set_state(op, "done", note="reverted staging")
        except OSError:
            pass

    # ---------------------------------------------------------------- content ops
    def _final_path(self, it: PlanItem) -> str:
        """Where the target file is now (a companion that was moved earlier in this batch)."""
        orig = it.payload.get("original_source")
        for old, new in reversed(self._moved):
            if old == orig:
                return new
        if self.fs.exists(it.source):
            return it.source
        return orig or it.source

    def _run_content_op(self, it: PlanItem):
        target = self._final_path(it)
        kind = "rewrite" if it.action == Action.REWRITE.value else "write_tags"
        op = self.j.add(Op(0, self.batch, it.id, kind, target, target, state="pending"))
        try:
            backup = self.j.backup_path(self.batch, f"{op.seq}-{os.path.basename(target)}")
            shutil.copy2(target, backup)
            with open(backup, "rb") as fh:
                os.fsync(fh.fileno())
            self.j.set_state(op, "backed_up", backup=backup, src_hash=hash_file(target))
            tmp = os.path.join(os.path.dirname(target), f".curator-edit-{self.batch}-{op.seq}{os.path.splitext(target)[1]}")
            shutil.copy2(target, tmp)
            self.j.set_state(op, "editing", tmp=tmp)
            if kind == "rewrite":
                rewrite_references(tmp, it.payload.get("replace", {}))
            else:
                from ..metadata.audio import write_audio_tags
                write_audio_tags(tmp, it.payload.get("values", {}))
            with open(tmp, "rb") as fh:
                os.fsync(fh.fileno())
            self.fs.replace_own(tmp, target)
            self.fs.fsync_dir(os.path.dirname(target))
            self.j.set_state(op, "done", after_hash=hash_file(target), dst_sig=_sig(self.fs.stat(target)))
            self.result.done += 1
        except (Cancelled, StorageUnavailable, DiskFull):
            raise
        except Exception as e:
            if op.tmp and os.path.exists(op.tmp):
                os.unlink(op.tmp)
            self.j.set_state(op, "failed", error=str(e))
            self._fail(it, f"could not update file contents: {e} (file unchanged)")

    def _cleanup_dirs(self):
        sources = list((self.plan.header("sources") or {}).keys())
        cands = set()
        for d in self._src_dirs:
            while any(is_within(d, s) and d != s for s in sources):
                cands.add(d)
                d = os.path.dirname(d)
        for d in sorted(cands, key=lambda p: -p.count(os.sep)):
            try:
                if self.fs.isdir(d) and not self.fs.listdir(d):
                    op = self.j.add(Op(0, self.batch, None, "rmdir", d, d, state="started"))
                    self.fs.rmdir(d)  # fails safely if anything appeared
                    self.j.set_state(op, "done")
            except OSError:
                continue


# ---------------------------------------------------------------------- shared primitives

def _sig(st: os.stat_result) -> dict[str, int]:
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "dev": st.st_dev}


def move_file(ex: Executor, op: Op, src: str, dst: str) -> None:
    """Journaled move. Same-volume: atomic no-replace rename. Cross-volume: verified copy."""
    j, fs = ex.j, ex.fs
    if not op.cross:
        j.set_state(op, "started")
        try:
            method = ex._retry(fs.rename_noreplace, src, dst, root=os.path.dirname(dst))
            fs.fsync_dir(os.path.dirname(dst))
            if os.path.dirname(src) != os.path.dirname(dst):
                fs.fsync_dir(os.path.dirname(src))
            j.set_state(op, "done", dst_sig=_sig(fs.stat(dst)), note=(op.note or "") + f" [{method}]")
            return
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            j.set_state(op, "pending", cross=True, note="different volume detected at rename time")
    copy_move(ex, op, src, dst)


def copy_move(ex: Executor, op: Op, src: str, dst: str, resume: bool = False) -> None:
    j, fs = ex.j, ex.fs
    tmp = op.tmp or ex.partial_for(op) or os.path.join(os.path.dirname(dst), f".curator-part-{op.batch}-{op.seq}")
    if op.state not in ("copied", "finalized"):
        j.set_state(op, "copying", tmp=tmp)
        try:
            h = ex._retry(_copy_resumable, ex, src, tmp, root=os.path.dirname(dst))
        except StorageUnavailable:
            raise  # keep the partial copy: Resume continues after its verified prefix
        except Exception as e:
            _safe_unlink(fs, tmp)
            j.set_state(op, "failed", error=f"copy failed: {e}; original kept")
            raise
        if ex.result:
            ex.result.bytes_copied += os.path.getsize(tmp)
        got = hash_file(tmp)
        if got != h:
            _safe_unlink(fs, tmp)
            j.set_state(op, "failed", error="copy verification failed")
            raise VerifyFailed("the copy did not match the original; original kept")
        notes = fs.copystat(src, tmp)
        j.set_state(op, "copied", src_hash=h, note="; ".join(notes) if notes else op.note)
    try:
        ex._retry(fs.rename_noreplace, tmp, dst, root=os.path.dirname(dst))
    except FileExistsError:
        _safe_unlink(fs, tmp)
        raise
    fs.fsync_dir(os.path.dirname(dst))
    j.set_state(op, "finalized")
    finish_source_removal(ex, op, src, dst)


def finish_source_removal(ex: Executor, op: Op, src: str, dst: str) -> None:
    j, fs = ex.j, ex.fs
    if hash_file(dst) != op.src_hash:
        j.set_state(op, "failed", error="final copy does not match; original kept")
        raise VerifyFailed("final copy does not match the original; original kept")
    try:
        st = fs.stat(src)
        sig = op.src_sig or {}
        changed = sig and (st.st_size != sig.get("size") or st.st_mtime_ns != sig.get("mtime_ns"))
        if changed:
            # Source changed while we copied: the copy is stale. Remove our copy, keep the original.
            _safe_unlink(fs, dst)
            j.set_state(op, "failed", error="the original changed during the copy; copy removed, original kept")
            raise SourceChanged("the original changed during the copy; original kept")
        ex._retry(fs.unlink, src, root=os.path.dirname(src))
        fs.fsync_dir(os.path.dirname(src))
    except FileNotFoundError:
        pass
    j.set_state(op, "done", dst_sig=_sig(fs.stat(dst)))


def _safe_unlink(fs, p):
    try:
        fs.unlink(p)
    except OSError:
        pass


def _copy_resumable(ex: Executor, src: str, tmp: str) -> str:
    """Copy src to tmp, resuming after a verified prefix if tmp already exists. Returns source hash."""
    fs = ex.fs
    h = hashlib.blake2b(digest_size=32)
    offset = 0
    if fs.exists(tmp):
        have = os.path.getsize(tmp)
        if have and have <= os.path.getsize(src):
            # verify the existing prefix byte-for-byte before trusting it
            if hash_file(tmp, limit=have) == hash_file(src, limit=have):
                with fs.open_read(src) as fh:
                    left = have
                    while left:
                        b = fh.read(min(CHUNK, left))
                        h.update(b)
                        left -= len(b)
                offset = have
        if offset == 0:
            fs.unlink(tmp)
    with fs.open_read(src) as fin:
        fin.seek(offset)
        out = fs.open_write(tmp, append=offset > 0)
        try:
            if offset:
                out.seek(offset)
                out.truncate()
            while True:
                ex.control.checkpoint()
                b = fin.read(CHUNK)
                if not b:
                    break
                out.write(b)
                h.update(b)
            out.flush()
            os.fsync(out.fileno())
        except Cancelled:
            out.close()
            _safe_unlink(fs, tmp)
            raise
        finally:
            if not out.closed:
                out.close()
    return h.hexdigest()


def rewrite_references(path: str, replace: dict[str, str]) -> None:
    with open(path, "rb") as fh:
        raw = fh.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
        enc = "utf-8"
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
        enc = "latin-1"
    for old, new in replace.items():
        text = re.sub(r'(FILE\s+")' + re.escape(old) + r'(")', lambda m: m.group(1) + new + m.group(2), text)
        text = re.sub(r"(^|[\\/])" + re.escape(old) + r"(\r?$)", lambda m: m.group(1) + new + m.group(2), text,
                      flags=re.M)
    data = text.encode(enc)
    if bom:
        data = b"\xef\xbb\xbf" + data
    with open(path, "wb") as fh:
        fh.write(data)


def fmt_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)


def reconnect_hint(path: str) -> str:
    if path.startswith("\\\\"):
        return ("Reconnect the share in File Explorer (or `net use`), then try again. "
                "Saved credentials are kept in Windows Credential Manager.")
    if os.name == "nt":
        return "If this is a mapped drive, reconnect it in File Explorer and try again."
    if os.uname().sysname == "Darwin":
        return "Reconnect with Finder ▸ Go ▸ Connect to Server (⌘K), then try again."
    return ("Remount the share (for example `gio mount smb://server/share` or your fstab entry), "
            "then try again. If the old mount is stale, unmount it first (`umount -l`).")


# ---------------------------------------------------------------------- recovery

@dataclass
class RecoveryReport:
    batches: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    attention: list[str] = field(default_factory=list)
    results: list[ApplyResult] = field(default_factory=list)


def recover(journal: Journal, catalog: Catalog | None = None, fs: LocalFS | None = None,
            resume: bool = False, sleep=time.sleep) -> RecoveryReport:
    """Bring every unfinished batch to a consistent state without discarding any source.

    Interrupted steps are either completed (when the data is verifiably in place) or
    rolled back (the original stays where it was). With resume=True the remaining
    items of an apply batch run again from its plan, reusing verified partial copies.
    """
    fs = fs or LocalFS()
    rep = RecoveryReport()
    for b in journal.unfinished():
        bid = b["id"]
        rep.batches.append(bid)
        keep_partials = resume and b["kind"] == "apply"
        for op in journal.ops(bid):
            if op.state in TERMINAL_STATES:
                continue
            _recover_op(journal, fs, op, rep, keep_partials)
        _unstage_orphans(journal, fs, bid, rep)
        if resume and b.get("plan_path") and os.path.exists(b["plan_path"]) and b["kind"] == "apply":
            plan = PlanStore(b["plan_path"])
            ex = Executor(plan, journal, catalog, fs=fs, resume_batch=bid, sleep=sleep, active_probe=False,
                          cleanup_empty_dirs=(b.get("options") or {}).get("cleanup_empty_dirs", False))
            r = ex.run()
            rep.results.append(r)
            rep.actions.append(f"Batch {bid}: resumed — {r.message}")
        else:
            journal.set_batch(bid, "interrupted",
                              "Recovered to a consistent state. Resume to finish the remaining files, or undo.")
    return rep


TERMINAL_STATES = {"done", "failed", "skipped", "conflict", "cancelled", "reverted"}


def _unstage_orphans(j: Journal, fs: LocalFS, bid: str, rep: RecoveryReport) -> None:
    """A file renamed to a temporary staging name whose final move did not happen goes back."""
    ops = j.ops(bid)
    moved = {o.item_id for o in ops if o.kind in ("move", "quarantine") and o.state == "done"}
    for o in ops:
        if o.kind == "stage" and o.state == "done" and o.item_id not in moved and not (o.note or "").startswith("revert"):
            if fs.exists(o.dst) and not fs.exists(o.src):
                fs.rename_noreplace(o.dst, o.src)
                j.set_state(o, "reverted", note="reverted: final move did not happen")
                rep.actions.append(f"Restored {o.src} from its temporary name")


def _recover_op(j: Journal, fs: LocalFS, op: Op, rep: RecoveryReport, keep_partials: bool = False) -> None:
    src_exists = fs.exists(op.src) if op.src else False
    dst_exists = fs.exists(op.dst) if op.dst else False
    if op.kind == "mkdir":
        j.set_state(op, "done" if fs.isdir(op.dst) else "reverted")
        return
    if op.kind == "rmdir":
        j.set_state(op, "done" if not fs.exists(op.dst) else "reverted")
        return
    if op.kind in ("rewrite", "write_tags"):
        if op.tmp and fs.exists(op.tmp):
            _safe_unlink(fs, op.tmp)
            rep.actions.append(f"Removed unfinished edit file {op.tmp}")
        cur = hash_file(op.dst) if dst_exists else None
        if cur is None:
            j.set_state(op, "failed", error="file missing after interruption")
            rep.attention.append(f"{op.dst} is missing; its backup is at {op.backup}")
        elif op.src_hash is None or cur == op.src_hash:
            j.set_state(op, "reverted", note="interrupted before the change; file unchanged")
        else:
            j.set_state(op, "done", after_hash=cur, note="change completed before interruption")
        return
    if not op.cross or op.kind == "stage":
        if src_exists and dst_exists and fs.samefile(op.src, op.dst) and op.src != op.dst:
            fs.unlink(op.src)  # interrupted link+unlink fallback: both names are the same data
            j.set_state(op, "done", dst_sig=_sig(fs.stat(op.dst)), note="finished interrupted link+unlink rename")
            rep.actions.append(f"Finished rename to {op.dst}")
        elif src_exists:
            j.set_state(op, "reverted", note="interrupted before the rename; nothing changed")
        elif dst_exists:
            j.set_state(op, "done", dst_sig=_sig(fs.stat(op.dst)), note="rename completed before interruption")
            rep.actions.append(f"Confirmed {op.dst}")
        else:
            j.set_state(op, "failed", error="neither the original nor the destination exists")
            rep.attention.append(f"Missing: {op.src} → {op.dst}. Check that the storage is connected.")
        return
    # cross-volume copy
    if op.state in ("pending", "copying", "started"):
        if op.tmp and fs.exists(op.tmp):
            if src_exists and keep_partials:
                j.set_state(op, "reverted", note="interrupted copy; partial kept for resume")
                rep.actions.append(f"Kept partial copy {op.tmp} to resume later; original untouched")
            elif src_exists:
                _safe_unlink(fs, op.tmp)
                j.set_state(op, "reverted", note="interrupted copy; partial copy removed, original kept")
                rep.actions.append(f"Removed partial copy {op.tmp}; original kept at {op.src}")
            else:
                j.set_state(op, "failed", error="original missing; partial copy kept for inspection")
                rep.attention.append(f"Original {op.src} is missing; a partial copy was kept at {op.tmp}.")
        else:
            j.set_state(op, "reverted", note="copy had not started")
        return
    if op.state == "copied":
        if op.tmp and fs.exists(op.tmp) and not dst_exists and hash_file(op.tmp) == op.src_hash:
            fs.rename_noreplace(op.tmp, op.dst)
            j.set_state(op, "finalized")
            dst_exists = True
            rep.actions.append(f"Finalized verified copy {op.dst}")
        else:
            if op.tmp:
                _safe_unlink(fs, op.tmp)
            j.set_state(op, "reverted", note="verified copy could not be finalized; original kept")
            return
    if op.state == "finalized":
        if dst_exists and hash_file(op.dst) == op.src_hash:
            if src_exists:
                st = fs.stat(op.src)
                if st.st_size == op.src_sig.get("size") and st.st_mtime_ns == op.src_sig.get("mtime_ns"):
                    fs.unlink(op.src)
                    rep.actions.append(f"Removed original after verified copy: {op.src}")
                else:
                    rep.attention.append(f"{op.src} changed after it was copied; both versions kept.")
                    j.set_state(op, "failed", error="original changed after copy; both kept")
                    return
            j.set_state(op, "done", dst_sig=_sig(fs.stat(op.dst)))
        else:
            j.set_state(op, "failed", error="destination copy missing or different; original kept")
            if not src_exists:
                rep.attention.append(f"Both {op.src} and a verified {op.dst} are unavailable.")
