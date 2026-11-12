"""Safe, deterministic, cancellable directory traversal.

Design goals (each fixes a weakness of ad-hoc ``os.walk`` loops):

* **Deterministic**: entries are visited in sorted order, depth-first, always.
* **Never aborts on one bad entry**: permission errors, vanished files and loops are
  recorded as `WalkIssue`s and the walk continues; callers decide whether the result is
  "complete" (an incomplete scan must never be used to delete catalog rows).
* **Cheap**: one ``stat`` per directory (cycle + mount check) and zero extra syscalls
  per file - sizes and mtimes come from the cached ``DirEntry`` data. No ``realpath``
  per file.
* **Safe by default**: symlinks and other filesystems are not followed unless asked.
* **Cancellable**: checks an event between directories and raises `WalkCancelled`.
"""
from __future__ import annotations

import os
import stat as stat_mod
import threading
from dataclasses import dataclass, field
from typing import Callable, Collection, Iterable, Iterator


class WalkCancelled(Exception):
    """Raised when the cancel event is set. The walk can simply be abandoned."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    name: str
    path: str
    size: int
    mtime_ns: int
    ino: int
    dev: int
    nlink: int


@dataclass(frozen=True, slots=True)
class WalkIssue:
    path: str
    kind: str      # denied | vanished | loop | mount | symlink | error
    message: str


@dataclass
class WalkReport:
    dirs: int = 0
    files: int = 0
    issues: list[WalkIssue] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """False if any part of the tree could not be read (loops/mounts/symlinks are policy, not damage)."""
        return not any(i.kind in ("denied", "vanished", "error") for i in self.issues)

    def of_kind(self, kind: str) -> list[WalkIssue]:
        return [i for i in self.issues if i.kind == kind]


@dataclass(frozen=True, slots=True)
class DirListing:
    path: str
    files: list[FileEntry]
    subdirs: list[str]


def _issue_kind(e: OSError) -> str:
    if isinstance(e, PermissionError):
        return "denied"
    if isinstance(e, FileNotFoundError):
        return "vanished"
    return "error"


def walk(root: str, *, follow_symlinks: bool = False, cross_mounts: bool = False,
         include_hidden: bool = False, skip_names: Collection[str] = (),
         skip_prefixes: tuple[str, ...] = (), prune: Callable[[str], str | None] | None = None,
         cancel: threading.Event | None = None, report: WalkReport | None = None) -> Iterator[DirListing]:
    """Yield one `DirListing` per readable directory under `root`.

    `prune(dirpath)` may return a reason string to skip that directory (it is recorded
    as a ``mount``-free policy skip by the caller if desired); returning None descends.
    Subdirectories are yielded in the listing so a caller can inspect them, but the walk
    itself decides what to descend into.
    """
    rep = report if report is not None else WalkReport()
    try:
        root_stat = os.stat(root)
    except OSError as e:
        rep.issues.append(WalkIssue(root, _issue_kind(e), e.strerror or str(e)))
        return
    root_dev = root_stat.st_dev
    seen: set[tuple[int, int]] = set()
    stack = [root]
    while stack:
        if cancel is not None and cancel.is_set():
            raise WalkCancelled()
        d = stack.pop()
        if prune is not None and prune(d):
            continue
        try:
            dst = os.stat(d)
        except OSError as e:
            rep.issues.append(WalkIssue(d, _issue_kind(e), e.strerror or str(e)))
            continue
        if not cross_mounts and dst.st_dev != root_dev:
            rep.issues.append(WalkIssue(d, "mount", "different filesystem not entered"))
            continue
        key = (dst.st_dev, dst.st_ino)
        if key in seen:
            rep.issues.append(WalkIssue(d, "loop", "directory already visited (symlink or bind-mount loop)"))
            continue
        seen.add(key)
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)  # fully consumed: handle closed deterministically
        except OSError as e:
            rep.issues.append(WalkIssue(d, _issue_kind(e), e.strerror or str(e)))
            continue
        rep.dirs += 1
        files: list[FileEntry] = []
        subdirs: list[str] = []
        for e in entries:
            name = e.name
            if name in skip_names or (skip_prefixes and name.startswith(skip_prefixes)):
                continue
            if not include_hidden and name.startswith("."):
                continue
            full = os.path.join(d, name)
            try:
                link = e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
                if link and not follow_symlinks:
                    rep.issues.append(WalkIssue(full, "symlink", "link not followed"))
                    continue
                if e.is_dir(follow_symlinks=follow_symlinks):
                    subdirs.append(full)
                    continue
                st = e.stat(follow_symlinks=follow_symlinks)
            except OSError as ex:
                rep.issues.append(WalkIssue(full, _issue_kind(ex), ex.strerror or str(ex)))
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            files.append(FileEntry(name, full, st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, st.st_nlink))
        rep.files += len(files)
        yield DirListing(d, files, subdirs)
        stack.extend(reversed(subdirs))  # reversed so pop() yields sorted order


def iter_files(roots: Iterable[str], **kw) -> Iterator[FileEntry]:
    """Convenience: every regular file under every root, in deterministic order."""
    for r in roots:
        for listing in walk(r, **kw):
            yield from listing.files
