"""Exact-duplicate removal plans (separate from organizing; requires explicit approval).

Duplicates are found by size then BLAKE2b content hash. Removal means moving the
extra copies into a quarantine folder, which is journaled and undoable; nothing
is deleted outright.
"""
from __future__ import annotations

import os

from ..models import Action, Flag, PlanItem
from ..storage.capabilities import inspect, volume_identity
from ..storage.catalog import Catalog
from .planstore import PlanStore
from .scanner import normalize_roots
from .util import hash_file, is_within, stat_sig


def plan_dedupe(catalog: Catalog, sources: list[str], quarantine_dir: str, plans_dir: str,
                prefer_roots: list[str] | None = None) -> PlanStore:
    sources = normalize_roots(sources)
    quarantine_dir = os.path.abspath(quarantine_dir)
    for s in sources:
        if is_within(quarantine_dir, s) is False and is_within(s, quarantine_dir):
            raise ValueError("The quarantine folder cannot contain a source folder.")
    store = PlanStore.new(plans_dir)
    store.set_header("kind", "dedupe")
    store.set_header("profile", {"sources": sources, "dedupe": {"quarantine_dir": quarantine_dir}})
    store.set_header("sources", {r: volume_identity(r).to_dict() for r in sources if os.path.isdir(r)})
    caps = inspect(quarantine_dir)
    store.set_header("dest_caps", {quarantine_dir: caps.to_dict()})
    prefer = [os.path.abspath(p) for p in (prefer_roots or [])]
    qdev = None
    base = quarantine_dir
    while not os.path.exists(base):
        base = os.path.dirname(base)
    qdev = os.stat(base).st_dev
    rows = catalog.db.execute("SELECT size FROM files WHERE size>0 GROUP BY size HAVING COUNT(*)>1 ORDER BY size")
    sizes = [r[0] for r in rows]
    n = 0
    for size in sizes:
        paths = sorted(p for p in catalog.size_peers(size) if any(is_within(p, s) for s in sources)
                       and not is_within(p, quarantine_dir))
        if len(paths) < 2:
            continue
        by_hash: dict[str, list[str]] = {}
        for p in paths:
            try:
                st = os.stat(p)
            except OSError:
                continue
            sig = stat_sig(st)
            h = catalog.cached_hash(p, sig) or hash_file(p)
            catalog.store_hash(p, sig, h)
            by_hash.setdefault(h, []).append(p)
        for h, group in sorted(by_hash.items()):
            if len(group) < 2:
                continue
            group.sort(key=lambda p: (not any(is_within(p, r) for r in prefer), p.count(os.sep), p))
            keep, extra = group[0], group[1:]
            for p in extra:
                rec = catalog.get(p)
                n += 1
                root = next(s for s in sources if is_within(p, s))
                dest = os.path.join(quarantine_dir, os.path.basename(root), os.path.relpath(p, root))
                it = PlanItem(id=n, source=p, dest=dest, category="other", action=Action.QUARANTINE.value,
                              reason=f"Exact duplicate of {keep} (same {size}-byte content). Moves to quarantine.",
                              flags=[Flag.DUPLICATE.value], size=size, content_hash=h, dest_root=quarantine_dir,
                              dest_key=dest.casefold(),
                              src_sig={"size": rec.size, "mtime_ns": rec.mtime_ns, "ino": rec.ino, "dev": rec.dev}
                              if rec else {},
                              cross_device=(rec.dev if rec else -1) != qdev, group=f"dup:{h[:16]}")
                store.add(it)
    store.flush()
    store.set_header("summary", store.summary())
    return store
