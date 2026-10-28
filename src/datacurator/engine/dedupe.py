"""Cancellable exact-duplicate previews; extras move to an undoable quarantine."""
from __future__ import annotations

import copy
import os
from collections import Counter

from ..models import Action, Flag, PlanItem
from ..naming.sanitize import NamingRules, equivalence_key, sanitize_component
from ..storage.capabilities import inspect, volume_identity
from ..storage.catalog import Catalog
from .exclusions import DirectoryExclusions
from .planstore import PlanStore
from .scanner import normalize_roots
from .util import hash_file, is_within_real, stat_sig


def _sig(st):
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "ino": st.st_ino, "dev": st.st_dev}


def plan_dedupe(catalog: Catalog, sources: list[str], quarantine_dir: str, plans_dir: str,
                prefer_roots: list[str] | None = None, *, profile=None, kind=None, cancel=None, progress=None) -> PlanStore:
    sources = normalize_roots(sources)
    quarantine_dir = os.path.abspath(quarantine_dir)
    for source in sources:
        if is_within_real(source, quarantine_dir):
            raise ValueError("The quarantine folder cannot be a source folder or contain a source folder.")
    exclusions = DirectoryExclusions((profile or {}).get("scan"), roots=sources)
    excluded_files = catalog.exclusions()
    store = PlanStore.new(plans_dir)
    store.set_header("kind", "dedupe")
    saved_profile = copy.deepcopy(profile or {"sources": sources})
    saved_profile.setdefault("dedupe", {})["quarantine_dir"] = quarantine_dir
    store.set_header("profile", saved_profile)
    store.set_header("sources", {r: volume_identity(r).to_dict() for r in sources if os.path.isdir(r)})
    caps = inspect(quarantine_dir)
    store.set_header("dest_caps", {quarantine_dir: caps.to_dict()})
    prefer = [os.path.abspath(p) for p in (prefer_roots or [])]
    rules = NamingRules.from_dict(saved_profile.get("naming"))
    base = quarantine_dir
    while not os.path.exists(base):
        base = os.path.dirname(base)
    qdev = os.stat(base).st_dev
    with catalog._lock:
        sizes = [r[0] for r in catalog.db.execute(
            "SELECT size FROM files WHERE size>0 GROUP BY size HAVING COUNT(*)>1 ORDER BY size")]
    root_names = Counter(os.path.basename(r) for r in sources)
    labels = {r: (os.path.basename(r) if root_names[os.path.basename(r)] == 1 else
                  f"{i + 1}-{os.path.basename(r)}") for i, r in enumerate(sources)}
    used, new_dirs, groups, errors = set(), set(), [], []
    n = 0
    try:
        for index, size in enumerate(sizes):
            if cancel and cancel.is_set():
                raise InterruptedError("Duplicate search cancelled")
            if progress:
                progress(index, len(sizes))
            records = []
            for path in sorted(catalog.size_peers(size)):
                if not any(is_within_real(path, s) for s in sources) or is_within_real(path, quarantine_dir):
                    continue
                if exclusions.reason(os.path.dirname(path)) or any(is_within_real(path, e) for e in excluded_files):
                    continue
                rec = catalog.get(path)
                if rec and (not kind or rec.kind == kind):
                    records.append(rec)
            if len(records) < 2:
                continue
            by_hash = {}
            for rec in records:
                try:
                    st = os.stat(rec.path, follow_symlinks=False)
                    if os.path.islink(rec.path) or _sig(st) != rec.stat_sig:
                        errors.append(f"{rec.path}: changed since scanning")
                        continue
                    sig = stat_sig(st)
                    digest = catalog.cached_hash(rec.path, sig) or hash_file(rec.path, cancel=cancel)
                    if _sig(os.stat(rec.path, follow_symlinks=False)) != _sig(st):
                        errors.append(f"{rec.path}: changed while hashing")
                        continue
                    catalog.store_hash(rec.path, sig, digest)
                    by_hash.setdefault(digest, []).append((rec, st))
                except InterruptedError:
                    raise
                except OSError:
                    errors.append(f"{rec.path}: not readable")
            for digest, group in sorted(by_hash.items()):
                group.sort(key=lambda x: (not any(is_within_real(x[0].path, r) for r in prefer),
                                          x[0].path.count(os.sep), x[0].path))
                keeper, keeper_stat = group[0]
                # Hard links are already one physical copy; leave all linked names alone.
                extras = [(rec, st) for rec, st in group[1:]
                          if (st.st_dev, st.st_ino) != (keeper_stat.st_dev, keeper_stat.st_ino) and st.st_nlink == 1]
                if not extras:
                    continue
                group_id = f"dup:{digest[:16]}"
                groups.append({"id": group_id, "keep": keeper.path, "size": size,
                               "extras": [rec.path for rec, _ in extras]})
                for rec, st in extras:
                    n += 1
                    source = rec.path
                    root = next(s for s in sources if is_within_real(source, s))
                    components = [labels[root], *os.path.relpath(source, root).split(os.sep)]
                    safe = [sanitize_component(c, rules, caps, is_filename=i == len(components) - 1)[0]
                            for i, c in enumerate(components)]
                    dest = os.path.join(quarantine_dir, *safe)
                    stem, ext = os.path.splitext(dest)
                    count = 1
                    while os.path.lexists(dest) or equivalence_key(dest, caps, rules) in used:
                        count += 1
                        dest = f"{stem} ({count}){ext}"
                    key = equivalence_key(dest, caps, rules)
                    used.add(key)
                    directory = os.path.dirname(dest)
                    while not os.path.exists(directory):
                        new_dirs.add(directory)
                        directory = os.path.dirname(directory)
                    store.add(PlanItem(id=n, source=source, dest=dest, category="other",
                        action=Action.QUARANTINE.value, reason=f"Exact duplicate. Retain {keeper.path}; move this copy to quarantine.",
                        flags=[Flag.DUPLICATE.value], size=size, content_hash=digest, dest_root=quarantine_dir,
                        dest_key=key, src_sig=_sig(st), cross_device=st.st_dev != qdev, group=group_id,
                        payload={"keeper": keeper.path, "keeper_sig": _sig(keeper_stat)}))
        store.flush()
        store.set_header("duplicate_groups", groups)
        store.set_header("duplicate_errors", errors)
        store.set_header("duplicate_bytes", sum(g["size"] * len(g["extras"]) for g in groups))
        store.set_header("new_dirs", sorted(new_dirs))
        store.set_header("summary", store.summary())
        return store
    except BaseException:
        store.close()
        raise
