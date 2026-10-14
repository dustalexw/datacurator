"""Engine facade shared by the CLI and the GUI.

Workflow: choose sources → choose rules (profile) → scan → plan (review) → apply → results/undo.
"""
from __future__ import annotations

import os
import threading
from typing import Callable

from .. import profiles as profiles_mod
from ..storage.catalog import Catalog, default_data_dir
from .planner import Planner
from .planstore import PlanStore
from .scanner import ScanOptions, ScanStats, scan as scan_fn


class Engine:
    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or default_data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.plans_dir = os.path.join(self.data_dir, "plans")
        os.makedirs(self.plans_dir, exist_ok=True)
        self.catalog = Catalog(os.path.join(self.data_dir, "catalog.sqlite"))
        self._journal = None

    # ---------------------------------------------------------------- journal
    @property
    def journal(self):
        if self._journal is None:
            from .journal import Journal
            self._journal = Journal(os.path.join(self.data_dir, "journal.sqlite"),
                                    os.path.join(self.data_dir, "journal-backups"))
        return self._journal

    def close(self) -> None:
        self.catalog.close()
        if self._journal is not None:
            self._journal.close()

    # ------------------------------------------------------------------- scan
    def scan_options(self, profile: dict) -> ScanOptions:
        s = profile.get("scan", {})
        inventory_all = bool(s.get("inventory_all"))
        excl = [self.data_dir, *s.get("exclude_dirs", [])]
        # destination roots strictly inside a source root are output folders: never rescanned
        for c in profile["categories"].values():
            r = c.get("root")
            if r and not inventory_all:
                for src in profile["sources"]:
                    if r != src and r.startswith(src.rstrip(os.sep) + os.sep):
                        excl.append(r)
        q = (profile.get("dedupe") or {}).get("quarantine_dir")
        if q:
            excl.append(q)
        return ScanOptions(follow_symlinks=bool(s.get("follow_symlinks")), cross_mounts=inventory_all or bool(s.get("cross_mounts")),
                           include_hidden=inventory_all or bool(s.get("include_hidden")), exclude_dirs=excl,
                           exclude_games=False if inventory_all else s.get("exclude_games", True))

    def scan(self, profile: dict, progress: Callable[[str, int], None] | None = None,
             cancel: threading.Event | None = None) -> ScanStats:
        profile = profiles_mod.normalize(profile)
        return scan_fn(profile["sources"], self.catalog, self.scan_options(profile), progress, cancel)

    # ------------------------------------------------------------------- plan
    def plan(self, profile: dict, progress: Callable[[str, int], None] | None = None,
             cancel: threading.Event | None = None, caps_override=None, tv_provider=None, movie_provider=None) -> PlanStore:
        profile = profiles_mod.normalize(profile)
        return Planner(profile, self.catalog, self.plans_dir, progress, cancel, caps_override,
                       tv_provider=tv_provider, movie_provider=movie_provider).build()

    def open_plan(self, path_or_id: str) -> PlanStore:
        p = path_or_id
        if not os.path.exists(p):
            p = os.path.join(self.plans_dir, f"plan-{path_or_id}.sqlite")
        if not os.path.exists(p):
            raise FileNotFoundError(f"No plan {path_or_id}")
        return PlanStore(p)

    def list_plans(self) -> list[str]:
        return sorted((f for f in os.listdir(self.plans_dir) if f.endswith(".sqlite")), reverse=True)

    # ------------------------------------------------------------------ apply
    def find_duplicates(self, profile, kind=None, progress=None, cancel=None):
        from .dedupe import plan_dedupe
        profile = profiles_mod.normalize(profile)
        quarantine = profile["dedupe"].get("quarantine_dir") or os.path.join(self.data_dir, "duplicate-quarantine")
        prefer = profile["dedupe"].get("prefer_root")
        return plan_dedupe(self.catalog, profile["sources"], quarantine, self.plans_dir,
                           [prefer] if prefer else [], profile=profile, kind=kind, progress=progress, cancel=cancel)

    def apply(self, plan: PlanStore, progress=None, control=None, **kw):
        from .executor import Executor
        ex = Executor(plan, self.journal, self.catalog, progress=progress, control=control, **kw)
        return ex.run()

    def recover(self, **kw):
        from .executor import recover
        return recover(self.journal, self.catalog, **kw)

    def undo(self, batch_id: str, **kw):
        from .undo import undo_batch
        return undo_batch(self.journal, self.catalog, batch_id, **kw)
