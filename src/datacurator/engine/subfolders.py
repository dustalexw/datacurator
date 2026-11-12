"""Organize each subfolder of a chosen folder with its own preset structure.

The chosen folder ("parent") stays where it is. Every immediate subfolder is organized *inside
itself* by one of a few presets (`profiles.SUBFOLDER_LAYOUTS`): everything tidy, music library,
photos by date, documents by type, or leave as is. Subfolders that depend on their location (apps,
projects, libraries, games …) are always left as is. Files lying directly in the parent can
optionally get a preset too.

Subfolders that share a preset are planned together; each preset group is an ordinary plan, so
everything is previewed, journaled and undoable exactly like a single-folder run.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Mapping

from .. import profiles as P
from ..models import CuratorError, Kind, PreflightError, kind_for
from .exclusions import DirectoryExclusions
from .executor import ApplyResult
from .planstore import PlanStore

LEAVE = "leave"
SAMPLE_LIMIT = 3000          # files inspected per subfolder when suggesting a layout


@dataclass
class SubfolderInfo:
    path: str
    name: str
    files: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    protected: str | None = None          # why it must stay exactly as it is, if so

    @property
    def layout(self) -> str:
        return suggest_layout(self)


def list_subfolders(parent: str) -> list[str]:
    """Immediate, visible, non-link subfolders in sorted order."""
    out = []
    try:
        with os.scandir(parent) as it:
            for e in it:
                if e.name.startswith(".") or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    out.append(e.path)
    except OSError as e:
        raise CuratorError(f"Cannot read {parent}: {e.strerror}") from e
    return sorted(out, key=lambda p: os.path.basename(p).casefold())


def inspect_subfolder(path: str, exclusions: DirectoryExclusions, limit: int = SAMPLE_LIMIT) -> SubfolderInfo:
    """Cheap, bounded look inside a subfolder: how many files of which kind, and whether it is protected."""
    info = SubfolderInfo(path, os.path.basename(path))
    info.protected = exclusions.reason(path)
    if info.protected:
        return info
    stack = [path]
    while stack and info.files < limit:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if not exclusions.reason(e.path):
                                stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            kind = kind_for(os.path.splitext(e.name)[1]).value
                            info.kinds[kind] = info.kinds.get(kind, 0) + 1
                            info.files += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return info


def suggest_layout(info: SubfolderInfo, share: float = 0.6) -> str:
    """Pick the preset that fits what is inside: mostly music → music, mostly photos → photos …"""
    if info.protected or info.files == 0:
        return LEAVE
    total = info.files
    audio = info.kinds.get(Kind.AUDIO.value, 0)
    images = info.kinds.get(Kind.IMAGE.value, 0)
    docs = info.kinds.get(Kind.DOCUMENT.value, 0) + info.kinds.get(Kind.TEXT.value, 0)
    if audio / total >= share:
        return "music"
    if images / total >= share:
        return "photos"
    if docs / total >= share:
        return "documents"
    return "tidy"


def inspect_all(parent: str) -> list[SubfolderInfo]:
    subs = list_subfolders(parent)
    excl = DirectoryExclusions({"exclude_dirs": [], "exclude_games": True, "allow_dirs": []}, roots=[parent])
    return [inspect_subfolder(p, excl) for p in subs]


# ------------------------------------------------------------------ plan / apply / undo
@dataclass
class GroupPlan:
    layout: str
    sources: list[str]
    profile: dict
    store: PlanStore
    stats: object | None = None

    @property
    def label(self) -> str:
        return P.SUBFOLDER_LAYOUTS.get(self.layout, self.layout)


@dataclass
class GroupApply:
    batches: list[str] = field(default_factory=list)
    done: int = 0
    failed: int = 0
    skipped: int = 0
    bytes_copied: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    status: str = "completed"
    message: str = ""


def plan_groups(engine, parent: str, assignments: Mapping[str, str], loose: str = LEAVE,
                progress: Callable | None = None, cancel: threading.Event | None = None) -> list[GroupPlan]:
    """Scan and plan one group per preset. `assignments` maps subfolder path → layout key."""
    parent = os.path.abspath(parent)
    subs = list_subfolders(parent)
    for path, layout in assignments.items():
        if layout != LEAVE and layout not in P.SUBFOLDER_LAYOUTS:
            raise CuratorError(f"Unknown layout '{layout}' for {os.path.basename(path)}.")
    wanted: dict[str, list[str]] = {}
    for path in subs:
        layout = assignments.get(path, LEAVE)
        if layout != LEAVE:
            wanted.setdefault(layout, []).append(path)
    groups: list[tuple[str, list[str], dict]] = [
        (layout, paths, P.subfolder_profile(layout, paths)) for layout, paths in
        sorted(wanted.items(), key=lambda kv: list(P.SUBFOLDER_LAYOUTS).index(kv[0]))]
    if loose != LEAVE:   # files directly in the parent; subfolders are excluded so they are never touched here
        groups.append((loose, [parent], P.subfolder_profile(loose, [parent], exclude_dirs=subs)))
    out: list[GroupPlan] = []
    for layout, sources, profile in groups:
        stats = engine.scan(profile, progress=progress, cancel=cancel)
        if stats.cancelled:
            raise InterruptedError("Scan cancelled")
        out.append(GroupPlan(layout, sources, profile, engine.plan(profile, progress=progress, cancel=cancel), stats))
    return out


def apply_groups(engine, groups: list[GroupPlan], progress=None, control=None) -> GroupApply:
    """Apply each group in turn. A group that cannot start is reported and the others still run;
    cancellation or lost storage stops the remaining groups."""
    total = GroupApply()
    for g in groups:
        if g.store.count("changes") == 0:
            continue
        try:
            res: ApplyResult = engine.apply(g.store, progress=progress, control=control)
        except PreflightError as e:
            total.errors.append((", ".join(os.path.basename(s) for s in g.sources), str(e)))
            total.status = "completed_with_errors"
            continue
        total.batches.append(res.batch_id)
        total.done += res.done
        total.failed += res.failed
        total.skipped += res.skipped
        total.bytes_copied += res.bytes_copied
        total.errors.extend(res.preflight + res.errors)
        if res.status in ("cancelled", "interrupted", "failed"):
            total.status = res.status
            break
        if res.status != "completed":
            total.status = res.status
    total.message = f"{total.done} done, {total.failed} failed, {total.skipped} skipped."
    return total


def undo_groups(engine, batch_ids: list[str]) -> list:
    """Undo newest first, so overlapping changes unwind in the right order."""
    return [engine.undo(b) for b in reversed(batch_ids)]
