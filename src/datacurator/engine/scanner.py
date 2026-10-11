"""Read-only scanning with incremental metadata extraction.

The scanner never opens files for writing and never changes timestamps it can
avoid changing (tag readers open files read-only). It records results in the
catalog; planning happens separately.
"""
from __future__ import annotations

import os
import stat as stat_mod
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..metadata.audio import read_audio
from ..metadata.other import read_nfo, read_photo
from ..models import FileRecord, Kind, kind_for
from ..storage.capabilities import inspect
from ..storage.catalog import META_VERSION, Catalog

SKIP_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini", "$RECYCLE.BIN", "System Volume Information",
              "lost+found", "@eaDir", "#recycle", ".Trashes", ".Spotlight-V100", ".fseventsd",
              ".TemporaryItems", ".AppleDouble", "@Recycle", ".snapshot", ".zfs"}
APP_PREFIXES = (".curator-", ".curator_")


class Cancelled(Exception):
    pass


@dataclass
class ScanOptions:
    follow_symlinks: bool = False
    cross_mounts: bool = False
    include_hidden: bool = False
    exclude_dirs: list[str] = field(default_factory=list)   # absolute paths never descended into
    workers: int = 4
    network_workers: int = 2


@dataclass
class ScanStats:
    files: int = 0
    dirs: int = 0
    extracted: int = 0
    reused: int = 0
    removed: int = 0
    skipped_symlinks: int = 0
    skipped_mounts: list[str] = field(default_factory=list)
    cycles: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _needs_meta(kind: str, name: str) -> bool:
    return kind in (Kind.AUDIO.value, Kind.IMAGE.value, Kind.NFO.value)


def extract_meta(path: str, kind: str) -> tuple[dict, str | None]:
    if kind == Kind.AUDIO.value:
        m = read_audio(path)
    elif kind == Kind.IMAGE.value:
        m = read_photo(path)
    elif kind == Kind.NFO.value:
        m = read_nfo(path)
    else:
        return {}, None
    err = m.pop("_error", None)
    return m, err


def normalize_roots(roots: list[str]) -> list[str]:
    """Absolute, de-duplicated, without roots nested inside other roots."""
    rs = sorted({os.path.abspath(r) for r in roots})
    out: list[str] = []
    for r in rs:
        if not any(r == o or r.startswith(o.rstrip(os.sep) + os.sep) for o in out):
            out.append(r)
    return out


def scan(roots: list[str], catalog: Catalog, opts: ScanOptions | None = None,
         progress: Callable[[str, int], None] | None = None,
         cancel: threading.Event | None = None) -> ScanStats:
    opts = opts or ScanOptions()
    stats = ScanStats()
    roots = normalize_roots(roots)
    exclude = {os.path.abspath(p) for p in opts.exclude_dirs}
    scan_id = catalog.begin_scan(roots)
    completed = True
    for root in roots:
        if not os.path.isdir(root):
            stats.errors.append(f"{root}: folder not found or not reachable")
            completed = False
            continue
        caps = inspect(root)
        workers = opts.network_workers if caps.is_network else opts.workers
        try:
            ok = _scan_root(root, catalog, opts, stats, exclude, scan_id, workers, progress, cancel)
        except Cancelled:
            stats.cancelled = True
            completed = False
            break
        if ok:
            stats.removed += catalog.prune_unseen(root, scan_id)
        else:
            completed = False
    catalog.end_scan(scan_id, stats.to_dict(), completed)
    return stats


def _scan_root(root, catalog, opts, stats, exclude, scan_id, workers, progress, cancel) -> bool:
    root_dev = os.stat(root).st_dev
    visited: set[tuple[int, int]] = set()
    stack = [root]
    complete = True
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        while stack:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            d = stack.pop()
            try:
                dst = os.stat(d)
            except OSError as e:
                stats.errors.append(f"{d}: {e.strerror}")
                complete = False
                continue
            key = (dst.st_dev, dst.st_ino)
            if key in visited:
                stats.cycles.append(d)
                continue
            visited.add(key)
            stats.dirs += 1
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError as e:
                stats.errors.append(f"{d}: {e.strerror}")
                complete = False
                continue
            known = catalog.dir_index(d)
            batch: list[FileRecord] = []
            meta_jobs = []
            subdirs = []
            for e in entries:
                name = e.name
                if name in SKIP_NAMES or name.startswith(APP_PREFIXES):
                    continue
                if not opts.include_hidden and name.startswith("."):
                    continue
                full = os.path.join(d, name)
                try:
                    is_link = e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
                    if e.is_dir(follow_symlinks=False) or (is_link and e.is_dir(follow_symlinks=True)):
                        if is_link and not opts.follow_symlinks:
                            stats.skipped_symlinks += 1
                            continue
                        if full in exclude:
                            continue
                        if not opts.cross_mounts:
                            sdev = os.stat(full).st_dev
                            if sdev != root_dev:
                                stats.skipped_mounts.append(full)
                                continue
                        subdirs.append(full)
                        continue
                    if is_link:
                        # file symlinks are never organized; following them could move data outside the source
                        stats.skipped_symlinks += 1
                        continue
                    st = e.stat(follow_symlinks=False)
                    if not stat_mod.S_ISREG(st.st_mode):
                        continue
                except OSError as ex:
                    stats.errors.append(f"{full}: {ex.strerror}")
                    continue
                ext = os.path.splitext(name)[1]
                kind = kind_for(ext).value
                rec = FileRecord(path=full, root=root, size=st.st_size, mtime_ns=st.st_mtime_ns,
                                 ino=st.st_ino, dev=st.st_dev, nlink=st.st_nlink, kind=kind,
                                 via_symlink=d != os.path.realpath(d) and opts.follow_symlinks)
                stats.files += 1
                old = known.get(name)
                unchanged = (old is not None and old["size"] == st.st_size and old["mtime_ns"] == st.st_mtime_ns
                             and old["ino"] == st.st_ino and old["meta_version"] == META_VERSION)
                if unchanged:
                    stats.reused += 1
                    batch.append((rec, False))
                elif _needs_meta(kind, name):
                    meta_jobs.append((rec, pool.submit(extract_meta, full, kind)))
                else:
                    batch.append((rec, True))
            for rec, fut in meta_jobs:
                try:
                    rec.meta, rec.meta_error = fut.result()
                except Exception as ex:  # pragma: no cover
                    rec.meta, rec.meta_error = {}, f"metadata error: {ex}"
                stats.extracted += 1
                batch.append((rec, True))
            if batch:
                catalog.upsert_files((r, scan_id, changed) for r, changed in batch)
            if progress:
                progress(d, stats.files)
            stack.extend(reversed(subdirs))
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return complete
