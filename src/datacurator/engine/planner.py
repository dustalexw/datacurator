"""Planning: turns catalog records + a profile into an explicit operation plan.

Planning is read-only for sources (only directory listings and, for collision
checks, content hashes are read). Every decision is deterministic: input is
processed in sorted path order and every tie is broken by a stable sort.
"""
from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from ..metadata.audio import primary_artist, year_of
from ..metadata.video import (episode_code, parse_movie, parse_tv, subtitle_suffix)
from ..models import (ALT_AUDIO_EXT, Action, Category, FileRecord, Flag, Kind, PlanItem)
from ..naming.sanitize import NamingRules, equivalence_key, sanitize_component
from ..naming.template import Template
from ..storage.capabilities import Capabilities, inspect, volume_identity
from ..storage.catalog import Catalog
from .planstore import PlanStore
from .scanner import normalize_roots
from .util import DirListingCache, hash_file, is_within, stat_sig

VARIOUS = {"various artists", "various", "va", "v.a.", "v/a", "varios artistas", "verschiedene interpreten",
           "artistes divers", "compilation", "soundtrack artists"}
DISC_SUFFIX = re.compile(r"\s*[\(\[]?\s*(?:disc|disk|cd)\s*(\d{1,2})(?:\s*(?:of|/)\s*\d{1,2})?\s*[\)\]]?\s*$", re.I)
TRACK_FROM_NAME = re.compile(r"^(?:(\d)[-.])?(\d{1,3})(?:\s*[-.)_]\s*|\s+)(.+)$")
SERIES_ART = re.compile(r"^(tvshow\.nfo|poster|fanart|banner|clearart|clearlogo|landscape|logo|folder|"
                        r"season\d{2}(-specials)?-(poster|banner|fanart|landscape)|season-specials-poster|"
                        r"season-all-(poster|banner|fanart)|theme)(\.[a-z0-9]+)?$", re.I)
MOVIE_ART = re.compile(r"^(poster|fanart|banner|clearart|clearlogo|landscape|logo|folder|disc|cover|movie\.nfo|"
                       r"moviethumb)(\.[a-z0-9]+)?$", re.I)
SUB_DIRS = {"subs", "subtitles", "sub", "subtitle"}
SAMPLE = re.compile(r"(^|[ ._-])sample([ ._-]|$)", re.I)
PART_RE = re.compile(r"(?<![a-z])(?:cd|disc|part|pt)[ ._-]?(\d)(?!\d)", re.I)


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def strip_disc(album: str | None) -> tuple[str | None, int | None]:
    if not album:
        return album, None
    m = DISC_SUFFIX.search(album)
    if m and album[:m.start()].strip():
        return album[:m.start()].strip(" -–:"), int(m.group(1))
    return album, None


def _mode(values: list, default=None):
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return default
    c = Counter(vals)
    best = max(c.values())
    return sorted([v for v, n in c.items() if n == best], key=lambda x: str(x))[0]


@dataclass
class _Ctx:
    root: str
    caps: Capabilities
    dev: int


@dataclass
class _Built:
    dest: str | None
    missing: list[str] = field(default_factory=list)
    sanitized: list[dict] = field(default_factory=list)
    error: str | None = None
    root: str | None = None


class Planner:
    def __init__(self, profile: dict, catalog: Catalog, plans_dir: str,
                 progress: Callable[[str, int], None] | None = None,
                 cancel: threading.Event | None = None,
                 caps_override: dict[str, Capabilities] | None = None, tv_provider=None):
        self.p = profile
        self.cat = catalog
        self.plans_dir = plans_dir
        self.progress = progress
        self.cancel = cancel
        self.rules = NamingRules.from_dict(profile.get("naming"))
        self.sources = normalize_roots(profile["sources"])
        self.cfg = profile["categories"]
        self.tpl = {c: (Template(v["folder"], "folder", c), Template(v["filename"], "filename", c))
                    for c, v in self.cfg.items()}
        self.overrides = catalog.overrides()
        self.excluded = catalog.exclusions()
        self.listing = DirListingCache()
        self._ctx: dict[str, _Ctx] = {}
        self._caps_override = caps_override or {}
        self._spelling: dict[tuple[str, str], str] = {}
        self.new_dirs: set[str] = set()
        self._next = 0
        self.claimed: set[str] = set()
        self.store: PlanStore | None = None
        # pass A summaries
        self.albums: dict[tuple, dict[str, Any]] = {}
        self.dir_groups: dict[tuple, dict[str, Any]] = {}
        self.series: dict[str, dict[str, Any]] = {}
        if tv_provider is None:
            from ..metadata.providers import make_tv_provider
            tv_provider = make_tv_provider((profile.get("providers") or {}).get("tv"), catalog)
        self.tv_provider = tv_provider
        self._matches: dict[tuple, Any] = {}
        self._episodes: dict[str, dict] = {}

    # ------------------------------------------------------------ utilities
    def _id(self) -> int:
        self._next += 1
        return self._next

    def ctx(self, root: str) -> _Ctx:
        c = self._ctx.get(root)
        if c is None:
            caps = self._caps_override.get(root)
            if caps is None:
                caps = inspect(root)
                cached = self.cat.cached_capabilities(root)
                if cached:
                    cc = Capabilities.from_dict(cached)
                    ok, _ = cc.identity.matches(caps.identity)
                    if ok:
                        cc.free_bytes = caps.free_bytes
                        caps = cc
            base = root
            while not os.path.exists(base):
                parent = os.path.dirname(base)
                if parent == base:
                    break
                base = parent
            try:
                dev = os.stat(base).st_dev
            except OSError:
                dev = -1
            c = _Ctx(root, caps, dev)
            self._ctx[root] = c
        return c

    def key(self, path: str, root: str) -> str:
        return equivalence_key(path, self.ctx(root).caps, self.rules)

    def _ovr(self, rec: FileRecord, group: str | None = None) -> dict[str, str]:
        o: dict[str, str] = {}
        if group and f"group:{group}" in self.overrides:
            o.update(self.overrides[f"group:{group}"])
        if rec.path in self.overrides:
            o.update(self.overrides[rec.path])
        return o

    def _is_excluded(self, path: str) -> bool:
        if not self.excluded:
            return False
        p = path
        while True:
            if p in self.excluded:
                return True
            parent = os.path.dirname(p)
            if parent == p:
                return False
            p = parent

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise InterruptedError("Planning cancelled")

    def dest_root(self, category: str, rec: FileRecord) -> str:
        r = self.cfg[category].get("root") or ""
        return r or rec.root

    # ---------------------------------------------------------- path building
    def _canonical_dir(self, root: str, comps: list[str]) -> tuple[str, str | None]:
        """Join components under root, reusing the spelling of existing folders and of
        folders already chosen in this plan, so equivalent names never fork a folder."""
        cur = root
        r = root
        while not os.path.isdir(r) and r not in self.new_dirs:
            self.new_dirs.add(r)
            parent = os.path.dirname(r)
            if parent == r:
                break
            r = parent
        for comp in comps:
            ck = self.key(comp, root)
            pk = self.key(cur, root)
            spelled = self._spelling.get((pk, ck))
            if spelled is None:
                lst = self.listing.listing(cur, lambda n: self.key(n, root))
                if lst is not None and ck in lst:
                    existing = lst[ck]
                    full = os.path.join(cur, existing)
                    if not os.path.isdir(full):
                        return os.path.join(cur, comp), f"a file named “{existing}” already exists where a folder is needed"
                    spelled = existing
                else:
                    spelled = comp
                self._spelling[(pk, ck)] = spelled
            cur = os.path.join(cur, spelled)
            if not os.path.isdir(cur):
                self.new_dirs.add(cur)
        return cur, None

    def build_path(self, category: str, rec: FileRecord, folder_vals: dict, file_vals: dict,
                   *, keep_name: bool = False, new_name: str | None = None, mode: str | None = None) -> _Built:
        cfg = self.cfg[category]
        mode = mode or cfg["mode"]
        ftpl, ntpl = self.tpl[category]
        src_dir, src_name = os.path.split(rec.path)
        stem, ext = os.path.splitext(src_name)
        sanitized: list[dict] = []
        missing: list[str] = []
        if mode == "rename":
            root = rec.root          # capabilities come from the file's own volume
            dest_dir = src_dir
            comps: list[str] = []
        else:
            root = self.dest_root(category, rec)
            comps, miss = ftpl.render_folder(folder_vals)
            missing += miss
        ctx = self.ctx(root)
        caps = ctx.caps
        safe_comps = []
        for c in comps:
            s, reasons = sanitize_component(c, self.rules, caps, is_filename=False)
            if reasons:
                sanitized.append({"component": "folder", "before": c, "after": s, "reasons": reasons})
            safe_comps.append(s)
        # filename
        if new_name is not None:
            name_stem, name_ext = os.path.splitext(new_name)
        elif keep_name or mode == "move":
            name_stem, name_ext = stem, ext
        else:
            rendered, miss = ntpl.render_filename(file_vals, ext)
            missing += miss
            name_stem, name_ext = rendered[: len(rendered) - len(ext)] if ext else rendered, ext
        s, reasons = sanitize_component(name_stem, self.rules, caps, is_filename=True, ext=name_ext)
        if reasons:
            sanitized.append({"component": "filename", "before": name_stem + name_ext, "after": s + name_ext,
                              "reasons": reasons})
        name = s + name_ext
        if missing:
            return _Built(None, missing=sorted(set(missing)), sanitized=sanitized, root=root)
        if mode != "rename":
            dest_dir, err = self._canonical_dir(root, safe_comps)
            if err:
                return _Built(None, sanitized=sanitized, error=err, root=root)
        dest = os.path.join(dest_dir, name)
        # path length
        limit = caps.max_path
        if len(dest) > limit:
            over = len(dest) - limit
            short = s[: max(1, len(s) - over)].rstrip(" .")
            if len(short) >= 8:
                sanitized.append({"component": "filename", "before": name, "after": short + name_ext,
                                  "reasons": [f"shortened so the full path fits the {limit}-character limit"]})
                name = short + name_ext
                dest = os.path.join(dest_dir, name)
            else:
                return _Built(None, sanitized=sanitized, root=root,
                              error=f"the destination path would exceed the {limit}-character limit")
        if not is_within(dest, root) or (mode == "rename" and os.path.dirname(dest) != src_dir):
            return _Built(None, sanitized=sanitized, root=root, error="generated path escapes the destination folder")
        return _Built(dest, sanitized=sanitized, root=root)

    def make_item(self, rec: FileRecord, category: str, built: _Built | None, reason: str, *,
                  role: str = "primary", parent_id: int | None = None, group: str = "",
                  provenance: dict | None = None, action: str | None = None,
                  flags: list[str] | None = None, suggestions: dict | None = None) -> PlanItem:
        it = PlanItem(id=self._id(), source=rec.path, dest=None, category=category, action=Action.REVIEW.value,
                      reason=reason, role=role, parent_id=parent_id, group=group,
                      flags=list(flags or []), size=rec.size, provenance=provenance or {},
                      suggestions=suggestions or {},
                      src_sig={"size": rec.size, "mtime_ns": rec.mtime_ns, "ino": rec.ino, "dev": rec.dev})
        if action is not None and action != Action.MOVE.value:
            it.action = action
            return it
        if built is None:
            return it
        it.sanitized = built.sanitized
        if built.sanitized:
            it.flags.append(Flag.SANITIZED.value)
        if built.error:
            it.action = Action.REVIEW.value
            it.flags.append(Flag.ERROR.value)
            it.reason = f"{reason} — cannot place: {built.error}"
            return it
        if built.dest is None:
            it.action = Action.REVIEW.value
            it.flags.append(Flag.UNCERTAIN.value)
            it.reason = "Missing information needed by the template: " + ", ".join(built.missing)
            return it
        it.dest = built.dest
        it.dest_root = built.root
        root_for_key = built.root or self.dest_root(category, rec)
        it.dest_key = self.key(built.dest, root_for_key)
        if built.dest == rec.path:
            it.action = Action.NONE.value
            it.reason = "Already organized: " + reason
        else:
            it.action = Action.MOVE.value
            ctx = self.ctx(root_for_key)
            it.cross_device = ctx.dev != rec.dev
            it.limitations = self._limitations(rec, ctx, it.cross_device)
            if it.limitations:
                it.flags.append(Flag.LIMITATION.value)
                if (self.p.get("preservation") or {}).get("block_lossy"):
                    it.action = Action.SKIP.value
                    it.reason = "Blocked by your preservation setting: " + "; ".join(it.limitations)
            if ctx.caps.read_only:
                it.action = Action.REVIEW.value
                it.flags.append(Flag.ERROR.value)
                it.reason = "Destination is read-only."
            mfs = ctx.caps.max_file_size
            if mfs and rec.size > mfs:
                it.action = Action.REVIEW.value
                it.flags.append(Flag.ERROR.value)
                it.reason = f"File is larger than the destination filesystem allows ({mfs // 1024**3} GiB limit)."
        return it

    def _limitations(self, rec: FileRecord, ctx: _Ctx, cross: bool) -> list[str]:
        lim: list[str] = []
        caps = ctx.caps
        if not cross:
            return lim
        if rec.nlink > 1:
            lim.append(f"this file has {rec.nlink} hard links; the copy will be a separate file")
        if os.name == "nt":
            lim.append("NTFS permissions (ACLs), alternate data streams and creation time are not copied")
        else:
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                lim.append("file owner/group become yours on the destination")
            if not hasattr(os, "listxattr") or caps.supports_xattr is False:
                lim.append("extended attributes are not copied")
            if os.uname().sysname == "Darwin":
                lim.append("creation date and Finder metadata may not be preserved")
        if caps.timestamp_resolution_ns >= 2_000_000_000:
            lim.append("modification time is rounded to 2 seconds (FAT)")
        if caps.fs_type in ("vfat", "msdos", "exfat", "fuse.exfat", "fat32"):
            lim.append("permissions are not stored on this filesystem")
        return lim

    # ------------------------------------------------------------- driver
    def build(self) -> PlanStore:
        store = PlanStore.new(self.plans_dir)
        self.store = store
        store.set_header("profile", self.p)
        store.set_header("sources", {r: (volume_identity(r).to_dict() if os.path.isdir(r) else None)
                                     for r in self.sources})
        # Pass A: album and series summaries
        n = 0
        for d, recs in self.cat.iter_dirs(self.sources):
            self._check_cancel()
            self._pass_a(d, recs)
            n += len(recs)
        self._finalize_albums()
        # Pass B: items
        n = 0
        for d, recs in self.cat.iter_dirs(self.sources):
            self._check_cancel()
            self._plan_dir(d, recs)
            n += len(recs)
            if self.progress:
                self.progress(d, n)
        store.flush()
        self._resolve_plan_collisions()
        self._resolve_existing_collisions()
        self._propagate_companions()
        store.flush()
        dest_roots = sorted({r for r in (self.cfg[c].get("root") for c in self.cfg) if r} | set(self.sources))
        store.set_header("dest_caps", {r: self.ctx(r).caps.to_dict() for r in dest_roots})
        store.set_header("new_dirs", sorted(self.new_dirs))
        store.set_header("summary", store.summary())
        return store

    # ------------------------------------------------------------- classification
    def _dir_records(self, d: str) -> list[FileRecord]:
        recs = []
        for sub in self._subtitle_dirs(d):
            for _, rs in self.cat.iter_dirs([sub]):
                recs.extend(r for r in rs if os.path.dirname(r.path) == sub)
        return recs

    def _subtitle_dirs(self, d: str) -> list[str]:
        lst = self.listing.listing(d, lambda n: n.casefold())
        if not lst:
            return []
        return [os.path.join(d, lst[k]) for k in sorted(lst) if k in SUB_DIRS and os.path.isdir(os.path.join(d, lst[k]))]

    def _classify(self, d: str, recs: list[FileRecord]) -> dict[str, Any]:
        out: dict[str, Any] = {"videos": [], "vcomp": {}, "audio": [], "mcomp": [], "photos": [],
                               "documents": [], "other": [], "series_art": [], "skip": []}
        recs = [r for r in recs if r.path not in self.claimed]
        videos = [r for r in recs if r.kind == Kind.VIDEO.value]
        tv_on = self.cfg["tv"]["enabled"]
        movies_on = self.cfg["movies"]["enabled"]
        # include Subs/ folders' files as potential video companions
        extra = self._dir_records(d) if videos else []
        stems = sorted(((os.path.splitext(os.path.basename(v.path))[0], v) for v in videos),
                       key=lambda t: -len(t[0]))
        for v in videos:
            stem = os.path.splitext(os.path.basename(v.path))[0]
            if SAMPLE.search(stem) and v.size < 300 * 1024 * 1024:
                out["skip"].append((v, "sample clip"))
                continue
            out["videos"].append(v)
        video_set = {v.path for v in out["videos"]}
        for r in recs + extra:
            if r.kind == Kind.VIDEO.value:
                continue
            name = os.path.basename(r.path)
            matched = None
            for stem, v in stems:
                if v.path not in video_set:
                    continue
                if name.startswith(stem) and len(name) > len(stem) and name[len(stem)] in ".-_ ":
                    matched = (v, name[len(stem):])
                    break
            if matched is None and r.kind == Kind.SUBTITLE.value:
                info = parse_tv(os.path.splitext(name)[0], [])
                if info:
                    cands = []
                    for v in out["videos"]:
                        vi = parse_tv(os.path.splitext(os.path.basename(v.path))[0], [])
                        if vi and vi.get("season") == info.get("season") and vi.get("episodes") == info.get("episodes"):
                            cands.append(v)
                    if len(cands) == 1:
                        matched = (cands[0], subtitle_suffix(os.path.splitext(name)[0]) + os.path.splitext(name)[1])
            if matched is None and r.kind == Kind.SUBTITLE.value and r in extra and len(out["videos"]) == 1:
                # a single video with a Subs/ folder: every subtitle belongs to it
                matched = (out["videos"][0], "." + os.path.splitext(name)[0] + os.path.splitext(name)[1])
            if matched:
                out["vcomp"][r.path] = (r, matched[0], matched[1])
                if r in extra:
                    self.claimed.add(r.path)
                continue
            if r in extra:
                continue
            out.setdefault("rest", []).append(r)
        has_audio = any(r.kind == Kind.AUDIO.value for r in out.get("rest", []))
        for r in out.get("rest", []):
            name = os.path.basename(r.path)
            if r.kind == Kind.AUDIO.value:
                if self.cfg["music"]["enabled"]:
                    out["audio"].append(r)
                else:
                    out["skip"].append((r, "music organizing is turned off"))
            elif has_audio and r.kind in (Kind.IMAGE.value, Kind.CUE.value, Kind.PLAYLIST.value, Kind.TEXT.value,
                                          Kind.NFO.value) or (has_audio and r.kind == Kind.DOCUMENT.value
                                                             and name.lower().endswith(".pdf")):
                out["mcomp"].append(r)
            elif out["videos"] and (SERIES_ART.match(name) or MOVIE_ART.match(name)):
                out["series_art"].append(r)
            elif r.kind == Kind.IMAGE.value:
                out["photos"].append(r)
            elif r.kind == Kind.DOCUMENT.value:
                out["documents"].append(r)
            else:
                out["other"].append(r)
        return out

    # ------------------------------------------------------------------ pass A
    def _music_tags(self, rec: FileRecord, group: str | None = None) -> dict[str, Any]:
        m = dict(rec.meta or {})
        prov = {k: m.get("_source", "tag") for k in ("title", "artist", "album", "album_artist", "track", "disc",
                                                         "date") if m.get(k) not in (None, "")}
        o = self._ovr(rec, group)
        for k, v in o.items():
            if k in ("track", "disc", "track_total", "disc_total"):
                try:
                    m[k] = int(v)
                except ValueError:
                    continue
            elif k == "compilation":
                m[k] = v.strip().lower() in ("1", "true", "yes")
            elif k == "year":
                m["date"] = v
            else:
                m[k] = v
            prov[k if k != "year" else "date"] = "your override"
        m["_prov"] = prov
        album, disc_from_title = strip_disc(m.get("album"))
        if disc_from_title and album != m.get("album"):
            m["album"] = album
            if not m.get("disc"):
                m["disc"] = disc_from_title
                prov["disc"] = "album title"
        return m

    def _resolve_album_artist(self, tracks: list[dict]) -> dict[str, Any]:
        comp_name = self.cfg["music"].get("compilation_name") or "Various Artists"
        aas = sorted({t["album_artist"] for t in tracks if t.get("album_artist")})
        comp = any(t.get("compilation") for t in tracks)
        if len(aas) == 1:
            aa = aas[0]
            if norm(aa) in VARIOUS:
                return {"aa": comp_name, "compilation": True, "prov": "album artist tag (compilation)"}
            return {"aa": aa, "compilation": comp, "prov": "album artist tag"}
        if len(aas) > 1:
            prim = sorted({primary_artist(a) for a in aas})
            if len(prim) == 1:
                return {"aa": prim[0], "compilation": comp, "prov": "album artist tag (featured artists removed)"}
            if all(norm(a) in VARIOUS for a in aas):
                return {"aa": comp_name, "compilation": True, "prov": "album artist tag (compilation)"}
            return {"aa": None, "status": "review",
                    "reason": "Tracks disagree on the album artist: " + ", ".join(f"“{a}”" for a in aas),
                    "suggestions": {"album_artist": _mode([t.get("album_artist") for t in tracks])}}
        if comp:
            return {"aa": comp_name, "compilation": True, "prov": "compilation flag"}
        prims = [primary_artist(t["artist"]) for t in tracks if t.get("artist")]
        uniq = sorted(set(prims))
        if len(uniq) == 1 and len(prims) == len(tracks):
            return {"aa": uniq[0], "compilation": False, "prov": "artist tag (no album artist tag)"}
        if not uniq:
            return {"aa": None, "status": "review", "reason": "No artist or album artist information."}
        if len(uniq) == 1:
            return {"aa": None, "status": "review",
                    "reason": "Some tracks have no artist tag; album artist cannot be determined safely.",
                    "suggestions": {"album_artist": uniq[0]}}
        return {"aa": None, "status": "review",
                "reason": f"Tracks have {len(uniq)} different artists and no album artist or compilation tag.",
                "suggestions": {"compilation": "1", "_note": f"or set album_artist (most common: {_mode(prims)})"}}

    def _pass_a(self, d: str, recs: list[FileRecord]) -> None:
        cls = self._classify_light(recs)
        if cls["audio"] and self.cfg["music"]["enabled"]:
            groups: dict[tuple, list[tuple[FileRecord, dict]]] = defaultdict(list)
            for r in cls["audio"]:
                pre = self._music_tags(r)
                gkey = (d, norm(pre.get("album")), pre.get("mb_albumid") or "")
                grp = f"music:{d}|{gkey[1]}"
                t = self._music_tags(r, grp)
                groups[gkey].append((r, t))
            for gkey, members in sorted(groups.items()):
                tags = [t for _, t in members]
                res = self._resolve_album_artist(tags)
                album = _mode([t.get("album") for t in tags])
                year = _mode([year_of(t.get("date")) for t in tags])
                info = {"aa": res.get("aa"), "compilation": res.get("compilation", False),
                        "status": res.get("status", "ok"), "reason": res.get("reason"),
                        "suggestions": res.get("suggestions") or {}, "aa_prov": res.get("prov"),
                        "album": album, "year": year, "group": f"music:{d}|{gkey[1]}"}
                if res.get("aa") and album:
                    prims = [primary_artist(t.get("artist")) for t in tags if t.get("artist")]
                    info["folder_artist"] = (res["aa"] if res.get("compilation") else (_mode(prims) or res["aa"]))
                    gk = (norm(res["aa"]), norm(album), gkey[2] or (year or ""))
                    info["global"] = gk
                    a = self.albums.setdefault(gk, {"discs": set(), "disc_total": 0, "album": album,
                                                    "aa": res["aa"], "year": year, "dirs": set()})
                    for t in tags:
                        if t.get("disc"):
                            a["discs"].add(t["disc"])
                        a["disc_total"] = max(a["disc_total"], t.get("disc_total") or 0)
                    a["dirs"].add(d)
                self.dir_groups[gkey] = info
        for r in cls["videos"]:
            stem = os.path.splitext(os.path.basename(r.path))[0]
            info = parse_tv(stem, self._parents(r))
            if info and info.get("series"):
                o = self._ovr(r)
                name = o.get("series") or info["series"]
                k = norm(name)
                s = self.series.setdefault(k, {"spellings": Counter(), "years": set()})
                s["spellings"][name] += 1
                y = o.get("series_year") or info.get("series_year")
                if y:
                    s["years"].add(int(y))

    def _classify_light(self, recs: list[FileRecord]) -> dict[str, list[FileRecord]]:
        videos = [r for r in recs if r.kind == Kind.VIDEO.value]
        vstems = sorted((os.path.splitext(os.path.basename(v.path))[0] for v in videos), key=len, reverse=True)
        audio = []
        for r in recs:
            if r.kind != Kind.AUDIO.value:
                continue
            name = os.path.basename(r.path)
            if any(name.startswith(s) and len(name) > len(s) and name[len(s)] in ".-_ " for s in vstems):
                continue
            audio.append(r)
        return {"videos": videos, "audio": audio}

    def _finalize_albums(self) -> None:
        """Detect different releases that would share one album folder and disambiguate by year."""
        by_folder: dict[tuple, list[tuple]] = defaultdict(list)
        for gk, a in self.albums.items():
            by_folder[(norm(a["aa"]), norm(a["album"]))].append(gk)
        for fk, gks in by_folder.items():
            if len(gks) < 2:
                continue
            years = [self.albums[g]["year"] for g in gks]
            if all(years) and len(set(years)) == len(years):
                for g in gks:
                    self.albums[g]["disambig"] = f"{self.albums[g]['album']} ({self.albums[g]['year']})"
            else:
                for g in gks:
                    self.albums[g]["clash"] = True

    def _parents(self, rec: FileRecord) -> list[str]:
        rel = os.path.relpath(os.path.dirname(rec.path), rec.root)
        if rel == ".":
            return [os.path.basename(rec.root)]
        parts = rel.split(os.sep)
        return list(reversed(parts)) + [os.path.basename(rec.root)]

    # ------------------------------------------------------------------ pass B
    def _plan_dir(self, d: str, recs: list[FileRecord]) -> None:
        store = self.store
        assert store is not None
        live = []
        for r in recs:
            if r.path in self.claimed:
                continue
            if self._is_excluded(r.path):
                store.add(self.make_item(r, "other", None, "Excluded by you.", action=Action.SKIP.value,
                                         flags=[Flag.EXCLUDED.value]))
                continue
            forced = self._ovr(r).get("category")
            if forced == "ignore":
                store.add(self.make_item(r, "other", None, "Marked “leave alone” by you.", action=Action.SKIP.value,
                                         flags=[Flag.EXCLUDED.value]))
                continue
            live.append(r)
        c = self._classify(d, live)
        for r, why in c["skip"]:
            store.add(self.make_item(r, "other", None, f"Left in place ({why}).", action=Action.SKIP.value))
        if c["audio"]:
            self._plan_music(d, c["audio"], c["mcomp"])
        else:
            for r in c["mcomp"]:
                c["other"].append(r)
        video_items = self._plan_videos(d, c["videos"], c["vcomp"], c["series_art"])
        for r in c["photos"]:
            self._plan_simple("photos", r)
        for r in c["documents"]:
            self._plan_simple("documents", r)
        for r in c["other"]:
            self._plan_simple("other", r)

    # ------------------------------------------------------------------ music
    def _plan_music(self, d: str, audio: list[FileRecord], comps: list[FileRecord]) -> None:
        store = self.store
        cfg = self.cfg["music"]
        groups: dict[tuple, list[tuple[FileRecord, dict]]] = defaultdict(list)
        for r in audio:
            pre = self._music_tags(r)
            gkey = (d, norm(pre.get("album")), pre.get("mb_albumid") or "")
            groups[gkey].append((r, self._music_tags(r, f"music:{d}|{gkey[1]}")))
        track_items: list[tuple[FileRecord, PlanItem]] = []
        cue_info = self._cue_refs(comps)
        image_files = {ref for refs, multi in cue_info.values() if multi for ref in refs}
        for gkey, members in sorted(groups.items()):
            info = self.dir_groups.get(gkey, {})
            group = info.get("group", f"music:{d}|{gkey[1]}")
            album = info.get("album")
            if not album:
                singles = cfg.get("singles_folder")
                if singles and info.get("aa"):
                    album = singles
                else:
                    sugg = self._path_suggestions(members)
                    for r, t in members:
                        reason = "No album tag." if t.get("has_tags") or t.get("title") else \
                            ("No readable tags." + (f" ({r.meta_error})" if r.meta_error else ""))
                        if sugg:
                            reason += " Folder names suggest values you can accept."
                        store.add(self.make_item(r, "music", None, reason, group=group, flags=[Flag.UNCERTAIN.value],
                                                 suggestions=sugg.get(r.path, {})))
                    continue
            if info.get("status") == "review" or not info.get("aa"):
                for r, t in members:
                    store.add(self.make_item(r, "music", None, info.get("reason") or "Album artist unknown.",
                                             group=group, flags=[Flag.UNCERTAIN.value],
                                             suggestions=info.get("suggestions") or {}))
                continue
            a = self.albums.get(info.get("global"), {})
            if a.get("clash"):
                for r, t in members:
                    store.add(self.make_item(
                        r, "music", None, "Another release with the same artist and album title exists and they "
                        "cannot be told apart by year. Set a distinguishing album name or year.", group=group,
                        flags=[Flag.UNCERTAIN.value, Flag.CONFLICT.value]))
                continue
            album_disp = a.get("disambig") or a.get("album") or album
            multi = len(a.get("discs", ())) > 1 or a.get("disc_total", 0) > 1
            for r, t in members:
                prov = dict(t.get("_prov", {}))
                prov["album_artist"] = info.get("aa_prov") if "album_artist" not in prov or prov.get("album_artist") != "your override" else prov["album_artist"]
                stem, ext = os.path.splitext(os.path.basename(r.path))
                title = t.get("title")
                if not title:
                    title = stem
                    prov["title"] = "original filename (no title tag)"
                disc = t.get("disc") if multi else None
                if multi and not t.get("disc"):
                    store.add(self.make_item(r, "music", None, "Multi-disc album but this track has no disc number.",
                                             group=group, flags=[Flag.UNCERTAIN.value]))
                    continue
                year = info.get("year")
                vals = {"album_artist": info["aa"], "album": album_disp, "year": year, "disc": disc,
                        "disc_number": t.get("disc"), "disc_total": t.get("disc_total") or a.get("disc_total") or None,
                        "track": t.get("track"), "track_total": t.get("track_total"), "title": title,
                        "genre": t.get("genre"), "format": t.get("format"), "original_name": stem,
                        "release": t.get("release_comment"), "ext": ext, "extension": ext}
                folder_vals = dict(vals, artist=info.get("folder_artist") or info["aa"])
                file_vals = dict(vals, artist=t.get("artist") or info["aa"])
                keep = r.path in image_files
                built = self.build_path("music", r, folder_vals, file_vals, keep_name=keep)
                reason = self._music_reason(info, album_disp, t, multi)
                if keep:
                    reason += " Filename kept because a cue sheet refers to it."
                it = self.make_item(r, "music", built, reason, group=group, provenance=prov)
                store.add(it)
                track_items.append((r, it))
                if self.p.get("write_tags"):
                    self._maybe_tag_item(r, it, t, group)
        self._plan_music_companions(d, comps, track_items, cue_info)

    def _music_reason(self, info: dict, album: str, t: dict, multi: bool) -> str:
        parts = [f"Album “{album}” by {info['aa']}"]
        if info.get("compilation"):
            parts.append("(compilation kept together)")
        if multi:
            parts.append(f"disc {t.get('disc')}")
        if t.get("track"):
            parts.append(f"track {t['track']}")
        else:
            parts.append("no track number, so the name has no number prefix")
        return ", ".join(parts) + "."

    def _path_suggestions(self, members: list[tuple[FileRecord, dict]]) -> dict[str, dict]:
        if not self.cfg["music"].get("path_fallback", True):
            return {}
        out: dict[str, dict] = {}
        for r, t in members:
            rel = os.path.relpath(os.path.dirname(r.path), r.root)
            parts = [] if rel == "." else rel.split(os.sep)
            if len(parts) < 2:
                continue
            s: dict[str, Any] = {"album_artist": parts[-2], "album": strip_disc(parts[-1])[0]}
            stem = os.path.splitext(os.path.basename(r.path))[0]
            m = TRACK_FROM_NAME.match(stem)
            if m:
                s["track"] = str(int(m.group(2)))
                s["title"] = m.group(3).strip()
                if m.group(1):
                    s["disc"] = m.group(1)
            out[r.path] = s
        return out

    def _maybe_tag_item(self, r: FileRecord, it: PlanItem, t: dict, group: str) -> None:
        o = self._ovr(r, group)
        writable = {k: v for k, v in o.items() if k in ("title", "artist", "album", "album_artist", "genre",
                                                          "track", "disc")}
        if "year" in o:
            writable["date"] = o["year"]
        if not writable or it.action not in (Action.MOVE.value, Action.NONE.value):
            return
        tag = PlanItem(id=self._id(), source=it.dest or r.path, dest=None, category="music",
                       action=Action.WRITE_TAGS.value, reason="Write your overrides into the file's tags: " +
                       ", ".join(f"{k}={v}" for k, v in sorted(writable.items())), role="companion",
                       parent_id=it.id, group=group, payload={"values": writable, "original_source": r.path})
        self.store.add(tag)

    def _cue_refs(self, comps: list[FileRecord]) -> dict[str, tuple[list[str], bool]]:
        """cue path -> (referenced file names, is_single_file_image)."""
        out = {}
        for r in comps:
            if r.kind != Kind.CUE.value:
                continue
            try:
                with open(r.path, "rb") as fh:
                    raw = fh.read(512 * 1024)
                text = raw.decode("utf-8-sig", errors="replace")
            except OSError:
                continue
            refs = re.findall(r'^\s*FILE\s+"([^"]+)"', text, re.M | re.I) or \
                re.findall(r"^\s*FILE\s+(\S+)", text, re.M | re.I)
            tracks = len(re.findall(r"^\s*TRACK\s+\d+", text, re.M | re.I))
            d = os.path.dirname(r.path)
            full = [os.path.normpath(os.path.join(d, x)) for x in refs]
            out[r.path] = (full, len(set(full)) == 1 and tracks > 1)
        return out

    def _plan_music_companions(self, d: str, comps: list[FileRecord], tracks: list[tuple[FileRecord, PlanItem]],
                               cue_info: dict) -> None:
        if not comps:
            return
        store = self.store
        mode = self.cfg["music"]["mode"]
        settled = [it for _, it in tracks if it.action in (Action.MOVE.value, Action.NONE.value)]
        pending = [it for _, it in tracks if it.action == Action.REVIEW.value]
        dest_dirs = sorted({os.path.dirname(it.dest) for it in settled if it.dest})
        parent = settled[0] if settled else None
        renamed = {r.path: it.dest for r, it in tracks if it.dest and
                   os.path.basename(it.dest) != os.path.basename(r.path)}
        stem_map = {os.path.splitext(os.path.basename(r.path))[0]: it for r, it in tracks if it.dest}
        for r in comps:
            name = os.path.basename(r.path)
            stem, ext = os.path.splitext(name)
            linked = stem_map.get(stem)
            if linked is not None and r.kind in (Kind.TEXT.value, Kind.CUE.value) and ext.lower() in (".lrc", ".txt"):
                new_name = os.path.splitext(os.path.basename(linked.dest))[0] + ext
                built = self._companion_built("music", r, linked, new_name)
                store.add(self.make_item(r, "music", built, "Lyrics follow their track.", role="companion",
                                         parent_id=linked.id, group=linked.group))
                continue
            if pending or len(dest_dirs) != 1 or parent is None:
                why = ("tracks in this folder are waiting for review" if pending else
                       "tracks in this folder go to different albums" if len(dest_dirs) > 1 else
                       "no tracks in this folder are being organized")
                store.add(self.make_item(r, "music", None, f"Album companion left in place: {why}.",
                                         role="companion", parent_id=parent.id if parent else None,
                                         action=Action.REVIEW.value if pending or len(dest_dirs) > 1 else Action.SKIP.value,
                                         flags=[Flag.UNCERTAIN.value] if len(dest_dirs) > 1 else []))
                continue
            if mode == "rename":
                store.add(self.make_item(r, "music", None, "Companion stays with its tracks (rename-only mode).",
                                         role="companion", parent_id=parent.id, action=Action.NONE.value))
                continue
            built = self._companion_built("music", r, parent, name, dest_dir=dest_dirs[0])
            reason = {Kind.IMAGE.value: "Album artwork moves with its album.",
                      Kind.CUE.value: "Cue sheet moves with its album.",
                      Kind.PLAYLIST.value: "Playlist moves with its album.",
                      Kind.DOCUMENT.value: "Booklet moves with its album."}.get(r.kind, "Album companion file moves with its album.")
            it = self.make_item(r, "music", built, reason, role="companion", parent_id=parent.id, group=parent.group)
            if r.kind in (Kind.CUE.value, Kind.PLAYLIST.value) and it.action in (Action.MOVE.value, Action.NONE.value):
                self._handle_references(r, it, renamed, cue_info)
            store.add(it)

    def _companion_built(self, category: str, r: FileRecord, parent: PlanItem, new_name: str,
                         dest_dir: str | None = None) -> _Built:
        dest_dir = dest_dir or os.path.dirname(parent.dest)
        root = parent.dest_root or dest_dir
        caps = self.ctx(root).caps
        stem, ext = os.path.splitext(new_name)
        s, reasons = sanitize_component(stem, self.rules, caps, True, ext)
        san = [{"component": "filename", "before": new_name, "after": s + ext, "reasons": reasons}] if reasons else []
        dest = os.path.join(dest_dir, s + ext)
        if not is_within(dest, root):
            return _Built(None, error="generated path escapes the destination folder", root=root)
        return _Built(dest, sanitized=san, root=root)

    def _handle_references(self, r: FileRecord, it: PlanItem, renamed: dict[str, str], cue_info: dict) -> None:
        d = os.path.dirname(r.path)
        if r.kind == Kind.CUE.value:
            refs = cue_info.get(r.path, ([], False))[0]
        else:
            refs = []
            try:
                with open(r.path, "rb") as fh:
                    for line in fh.read(512 * 1024).decode("utf-8-sig", errors="replace").splitlines():
                        line = line.strip()
                        if line and not line.startswith("#") and not re.match(r"^[a-z]+://", line, re.I):
                            refs.append(os.path.normpath(os.path.join(d, line.replace("\\", os.sep))))
            except OSError:
                return
        affected = {os.path.basename(x): os.path.basename(renamed[x]) for x in refs if x in renamed}
        if not affected:
            return
        if self.p.get("companions", {}).get("update_references"):
            rw = PlanItem(id=self._id(), source=it.dest or r.path, dest=None, category="music",
                          action=Action.REWRITE.value, role="companion", parent_id=it.id, group=it.group,
                          reason="Update references to renamed tracks: " +
                          ", ".join(f"{a} → {b}" for a, b in sorted(affected.items())),
                          payload={"replace": affected, "original_source": r.path})
            self.store.add(rw)
        else:
            it.action = Action.REVIEW.value
            it.dest = None
            it.dest_key = None
            it.flags.append(Flag.UNCERTAIN.value)
            it.reason = ("Refers to files that will be renamed (" + ", ".join(sorted(affected)) +
                         "). Enable “Update references in cue sheets and playlists” or fix it manually.")

    # ------------------------------------------------------------------ video
    def _plan_videos(self, d: str, videos: list[FileRecord], vcomp: dict, art: list[FileRecord]) -> list[PlanItem]:
        store = self.store
        items: dict[str, PlanItem] = {}
        series_dirs: set[str] = set()
        movie_dirs: set[str] = set()
        for v in videos:
            stem = os.path.splitext(os.path.basename(v.path))[0]
            parents = self._parents(v)
            o = self._ovr(v)
            forced = o.get("category")
            info = parse_tv(stem, parents) if forced in (None, "tv") else None
            if forced == "tv" and info is None:
                info = {"pattern": "override", "episodes": []}
            nfo = self._nfo_for(v)
            if info is not None and self.cfg["tv"]["enabled"] and forced != "movies":
                it = self._plan_episode(v, info, o, nfo)
                if it.dest and it.action in (Action.MOVE.value, Action.NONE.value):
                    sd = os.path.dirname(it.dest)
                    if self.cfg["tv"]["mode"] != "rename" and re.match(r"^Season \d+$", os.path.basename(sd)):
                        sd = os.path.dirname(sd)
                    series_dirs.add(sd)
            else:
                minfo = parse_movie(stem, parents) if forced in (None, "movies") else None
                if forced == "movies" and minfo is None:
                    minfo = {}
                if minfo is not None and self.cfg["movies"]["enabled"]:
                    it = self._plan_movie(v, minfo, o, nfo)
                    if it.dest and it.action in (Action.MOVE.value, Action.NONE.value):
                        movie_dirs.add(os.path.dirname(it.dest))
                elif info is not None or minfo is not None:
                    which = "TV" if info is not None else "movie"
                    it = self.make_item(v, "tv" if info is not None else "movies", None,
                                        f"Left in place: {which} organizing is turned off.", action=Action.SKIP.value)
                else:
                    it = self.make_item(v, "movies", None,
                                        "Could not tell whether this is an episode or a movie: no SxxEyy/1x02/date "
                                        "pattern and no year. Choose a category or set a title and year.",
                                        flags=[Flag.UNCERTAIN.value],
                                        suggestions={"category": "tv or movies"})
            store.add(it)
            items[v.path] = it
        for path, (r, video, suffix) in sorted(vcomp.items()):
            parent = items.get(video.path)
            if parent is None:
                continue
            self._plan_video_companion(r, parent, suffix)
        targets = series_dirs or movie_dirs
        for r in art:
            if len(targets) == 1 and len(videos) >= 1 and (self.cfg["tv"]["mode"] != "rename"):
                tgt = next(iter(targets))
                first = next(iter(items.values()))
                built = self._companion_built(first.category, r, first, os.path.basename(r.path), dest_dir=tgt)
                store.add(self.make_item(r, first.category, built, "Series/movie artwork and info stay with the show.",
                                         role="companion", parent_id=first.id, group=first.group))
            else:
                store.add(self.make_item(r, "tv", None, "Artwork left in place: this folder holds more than one show "
                                         "or nothing here is being organized.", action=Action.SKIP.value))
        return list(items.values())

    def _nfo_for(self, v: FileRecord) -> dict:
        nfo_path = os.path.splitext(v.path)[0] + ".nfo"
        rec = self.cat.get(nfo_path)
        meta = dict(rec.meta) if rec and rec.meta else {}
        show = self.cat.get(os.path.join(os.path.dirname(v.path), "tvshow.nfo")) or \
            self.cat.get(os.path.join(os.path.dirname(os.path.dirname(v.path)), "tvshow.nfo"))
        if show and show.meta.get("nfo_type") == "tvshow":
            meta.setdefault("series", show.meta.get("series"))
            if show.meta.get("series_year"):
                meta.setdefault("series_year", show.meta.get("series_year"))
        return meta

    def _plan_episode(self, v: FileRecord, info: dict, o: dict, nfo: dict) -> PlanItem:
        cfg = self.cfg["tv"]
        prov: dict[str, str] = {}
        info = dict(info)
        for k in ("series", "series_year", "season", "episode_title"):
            if nfo.get(k) is None:
                continue
            if info.get(k) in (None, "") or (k == "series" and info.get("series_source") == "folder"):
                info[k] = nfo[k]
                prov[k] = "local .nfo"
        if nfo.get("episodes") and not info.get("episodes") and info.get("pattern") != "date":
            info["episodes"] = nfo["episodes"]
            prov["episode"] = "local .nfo"
        for k in ("series", "episode_title"):
            if k in o:
                info[k] = o[k]
                prov[k] = "your override"
        for k in ("season", "series_year", "absolute"):
            if k in o:
                try:
                    info[k] = int(o[k])
                    prov[k] = "your override"
                except ValueError:
                    pass
        if "episode" in o:
            try:
                eps = [int(x) for x in re.split(r"[,\s-]+", o["episode"]) if x]
                info["episodes"] = eps if "-" not in o["episode"] else list(range(eps[0], eps[-1] + 1))
                prov["episode"] = "your override"
                if info.get("pattern") in ("absolute", "override"):
                    info["pattern"] = "SxxEyy"
            except (ValueError, IndexError):
                pass
        for k in ("series", "season", "episode_title"):
            prov.setdefault(k, "filename" if k != "series" or info.get("series_source") != "folder" else "folder name")
        grp = f"tv:{norm(info.get('series'))}"
        if not info.get("series"):
            return self.make_item(v, "tv", None, "Episode pattern found but no series name in the file or folder names.",
                                  group=grp, flags=[Flag.UNCERTAIN.value], provenance=prov)
        if info.get("pattern") == "absolute" and cfg.get("numbering") != "absolute" and "absolute" not in o:
            return self.make_item(v, "tv", None,
                                  f"Only a bare episode number ({info.get('absolute')}) was found. Choose a numbering "
                                  "convention: set TV numbering to “absolute”, or give a season and episode.",
                                  group=grp, flags=[Flag.UNCERTAIN.value],
                                  suggestions={"absolute": str(info.get("absolute")), "_note": "or season + episode"})
        if info.get("pattern") == "override" and not info.get("episodes") and info.get("absolute") is None:
            return self.make_item(v, "tv", None, "Marked as TV, but season and episode are unknown.",
                                  group=grp, flags=[Flag.UNCERTAIN.value])
        # series canonical spelling and year
        s = self.series.get(norm(info["series"]))
        series = info["series"]
        if s and prov.get("series") != "your override":
            best = max(s["spellings"].values())
            series = sorted(n for n, c in s["spellings"].items() if c == best)[0]
            if series != info["series"]:
                prov["series"] += " (spelling unified across episodes)"
        year = info.get("series_year")
        if year is None and s and "{series_year" in cfg["folder"] + cfg["filename"]:
            if len(s["years"]) == 1:
                year = next(iter(s["years"]))
                prov["series_year"] = "other episodes of this series"
            elif len(s["years"]) > 1:
                return self.make_item(v, "tv", None,
                                      f"“{series}” matches several series ({', '.join(map(str, sorted(s['years'])))}). "
                                      "Choose which one this episode belongs to.", group=grp,
                                      flags=[Flag.UNCERTAIN.value],
                                      suggestions={"series_year": " or ".join(map(str, sorted(s["years"])))})
        if self.tv_provider is not None and info.get("pattern") in ("SxxEyy", "NxNN", "date", "override"):
            r = self._enrich(series, year, o.get("series_id"), info, prov)
            if r is not None:
                return self.make_item(v, "tv", None, r[0], group=f"tv:{norm(series)}", flags=[Flag.UNCERTAIN.value],
                                      provenance=prov, suggestions=r[1])
        if info.get("season") == 0:
            prov["season"] = prov.get("season", "filename") + " (specials)"
        code = episode_code(info)
        stem, ext = os.path.splitext(os.path.basename(v.path))
        vals = {"series": series, "series_year": year, "season": info.get("season"),
                "episode": ",".join(str(e) for e in info.get("episodes") or []) or None,
                "episode_code": code, "episode_title": info.get("episode_title"),
                "absolute": info.get("absolute"), "air_date": info.get("air_date"),
                "resolution": info.get("resolution"), "source": info.get("source"), "codec": info.get("codec"),
                "original_name": stem, "ext": ext, "extension": ext}
        built = self.build_path("tv", v, vals, vals)
        kind = {"SxxEyy": "episode", "NxNN": "episode", "date": "daily episode", "absolute": "absolute-numbered episode",
                "override": "episode"}.get(info.get("pattern"), "episode")
        eps = info.get("episodes") or []
        reason = f"{series}: {kind} {code}"
        if len(eps) > 1:
            reason += f" (multi-episode file: {len(eps)} episodes)"
        if info.get("season") == 0:
            reason += " — special, filed under Season 00"
        reason += "."
        return self.make_item(v, "tv", built, reason, group=f"tv:{norm(series)}", provenance=prov)

    def _enrich(self, series: str, year: int | None, series_id: str | None, info: dict, prov: dict):
        """Fill a missing episode title from the optional provider. Returns (reason, suggestions)
        when the user must choose between ambiguous matches; None otherwise."""
        from ..metadata.providers import Candidate, choose
        pv = self.tv_provider
        key = (norm(series), year, series_id)
        try:
            if key not in self._matches:
                if series_id:
                    sid = series_id.split(":", 1)[-1]
                    self._matches[key] = ("chosen", Candidate(pv.name, sid, series, year, "chosen by you"))
                else:
                    m = choose(pv.search(series), series, year)
                    self._matches[key] = ("chosen", m.chosen) if m.chosen else ("ambiguous", m)
            state, val = self._matches[key]
            if state == "ambiguous":
                if not val.candidates:
                    prov["episode_title"] = prov.get("episode_title", "filename") + f"; {pv.name}: no match"
                    return None
                return (f"{pv.name} found several shows named “{series}”. Choose the right one.",
                        {"series_id": " or ".join(c.ref for c in val.candidates), "_candidates":
                         [c.label() for c in val.candidates]})
            cand = val
            if cand.id not in self._episodes:
                self._episodes[cand.id] = pv.episodes(cand.id)
            eps = self._episodes[cand.id]
            if info.get("pattern") == "date":
                hit = eps.get(f"D{info.get('air_date')}")
            elif info.get("episodes"):
                hit = eps.get(f"S{info.get('season')}E{info['episodes'][0]}")
            else:
                hit = None
            if hit and hit.get("title") and not info.get("episode_title"):
                info["episode_title"] = hit["title"]
                prov["episode_title"] = f"{pv.name} (show {cand.id}, {cand.label()})"
        except Exception as e:  # network or parse errors: stay offline, say so
            prov["episode_title"] = prov.get("episode_title", "filename") + \
                f"; {getattr(pv, 'name', 'provider')} unavailable ({e.__class__.__name__})"
        return None

    def _plan_movie(self, v: FileRecord, info: dict, o: dict, nfo: dict) -> PlanItem:
        prov = {"title": info.get("title_source", "filename"), "year": info.get("title_source", "filename")}
        info = dict(info)
        if nfo.get("nfo_type") == "movie":
            for k in ("title", "year"):
                if nfo.get(k):
                    info[k] = nfo[k]
                    prov[k] = "local .nfo"
        for k in ("title", "edition"):
            if k in o:
                info[k] = o[k]
                prov[k] = "your override"
        if "year" in o and o["year"].isdigit():
            info["year"] = int(o["year"])
            prov["year"] = "your override"
        stem, ext = os.path.splitext(os.path.basename(v.path))
        part = None
        if m := PART_RE.search(stem):
            part = int(m.group(1))
        if not info.get("title") or not info.get("year"):
            return self.make_item(v, "movies", None, "Movie title or year is unknown.", flags=[Flag.UNCERTAIN.value],
                                  group=f"movie:{v.path}")
        vals = {"title": info["title"], "year": info["year"], "edition": info.get("edition"),
                "resolution": info.get("resolution"), "source": info.get("source"), "codec": info.get("codec"),
                "original_name": stem, "ext": ext, "extension": ext}
        built = self.build_path("movies", v, vals, vals)
        if part and built.dest:
            d, n = os.path.split(built.dest)
            s, e = os.path.splitext(n)
            built.dest = os.path.join(d, f"{s} - part{part}{e}")
        return self.make_item(v, "movies", built, f"Movie “{info['title']}” ({info['year']}).",
                              group=f"movie:{norm(info['title'])}:{info['year']}", provenance=prov)

    def _plan_video_companion(self, r: FileRecord, parent: PlanItem, suffix: str) -> None:
        store = self.store
        name = os.path.basename(r.path)
        kind_word = ("subtitle" if r.kind == Kind.SUBTITLE.value else
                     "alternate audio track" if os.path.splitext(name)[1].lower() in ALT_AUDIO_EXT else
                     "metadata file" if r.kind == Kind.NFO.value else
                     "artwork" if r.kind == Kind.IMAGE.value else "companion file")
        if parent.action in (Action.MOVE.value, Action.NONE.value) and parent.dest:
            new_name = os.path.splitext(os.path.basename(parent.dest))[0] + suffix
            built = self._companion_built(parent.category, r, parent, new_name)
            lang = subtitle_suffix(os.path.splitext(name)[0]) if r.kind == Kind.SUBTITLE.value else ""
            reason = f"{kind_word.capitalize()} stays with its video" + (f" (keeps “{lang}”)" if lang else "") + "."
            store.add(self.make_item(r, parent.category, built, reason, role="companion", parent_id=parent.id,
                                     group=parent.group))
        else:
            store.add(self.make_item(r, parent.category, None, f"{kind_word.capitalize()} stays with its video, "
                                     "which is not being moved.", role="companion", parent_id=parent.id,
                                     action=parent.action if parent.action in (Action.REVIEW.value, Action.SKIP.value)
                                     else Action.SKIP.value, group=parent.group))

    # ------------------------------------------------------------------ simple categories
    def _plan_simple(self, cat: str, r: FileRecord) -> None:
        store = self.store
        cfg = self.cfg[cat]
        if not cfg.get("enabled"):
            store.add(self.make_item(r, cat, None, f"Left in place: {cat} organizing is turned off.",
                                     action=Action.SKIP.value))
            return
        stem, ext = os.path.splitext(os.path.basename(r.path))
        o = self._ovr(r)
        if cat == "photos":
            m = r.meta or {}
            prov = {"date": "EXIF capture date"}
            year, month, day = m.get("year"), m.get("month"), m.get("day")
            if "year" in o:
                year, prov["date"] = int(o["year"]), "your override"
                month = int(o.get("month", month or 0)) or None
            if not year and cfg.get("date_fallback") == "mtime":
                import datetime
                dt = datetime.datetime.fromtimestamp(r.mtime_ns / 1e9)
                year, month, day = dt.year, dt.month, dt.day
                prov["date"] = "file modified time (you enabled this fallback)"
            if not year:
                store.add(self.make_item(r, cat, None, "No capture date in the photo's metadata.",
                                         flags=[Flag.UNCERTAIN.value],
                                         suggestions={"_note": "set a year/month, or enable the modified-time fallback"}))
                return
            vals = {"year": year, "month": month, "day": day, "camera_make": m.get("camera_make"),
                    "camera_model": m.get("camera_model"), "name": stem, "original_name": stem,
                    "ext": ext, "extension": ext}
            built = self.build_path(cat, r, vals, vals)
            store.add(self.make_item(r, cat, built, f"Photo taken {year}-{month or 0:02d}.", provenance=prov))
            return
        rel = os.path.relpath(os.path.dirname(r.path), r.root)
        parts = [] if rel == "." else rel.split(os.sep)
        vals = {"project": o.get("project") or (parts[0] if parts else None),
                "subpath": os.path.join(*parts) if parts else None, "name": o.get("name") or stem,
                "original_name": stem, "ext": ext, "extension": ext, "type": ext.lstrip(".").upper() or None}
        if cat == "documents":
            vals["year"] = None
        built = self.build_path(cat, r, vals, vals)
        why = (f"Document in project “{vals['project']}”." if cat == "documents" and vals["project"] else
               "Document." if cat == "documents" else "Other file.")
        store.add(self.make_item(r, cat, built, why, provenance={"project": "top-level folder"} if vals["project"] else {}))

    # ------------------------------------------------------------------ collisions
    def _hash(self, path: str) -> str | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        sig = stat_sig(st)
        h = self.cat.cached_hash(path, sig)
        if h:
            return h
        try:
            h = hash_file(path, self.cancel)
        except OSError:
            return None
        self.cat.store_hash(path, sig, h)
        return h

    def _same_content(self, a: str, b: str, size_a: int, size_b: int) -> tuple[bool, str | None]:
        if not self.p.get("detect_duplicates", True) or size_a != size_b:
            return False, None
        ha, hb = self._hash(a), self._hash(b)
        return (ha is not None and ha == hb), ha

    def _alternate(self, it: PlanItem, taken: set[str]) -> bool:
        d, n = os.path.split(it.dest)
        stem, ext = os.path.splitext(n)
        root = it.dest_root or d
        lst = self.listing.listing(d, lambda x: self.key(x, root)) or {}
        for i in range(2, 1000):
            cand = os.path.join(d, f"{stem} ({i}){ext}")
            k = self.key(cand, root)
            if k in taken or self.key(os.path.basename(cand), root) in lst:
                continue
            if self.store.scalar("SELECT 1 FROM items WHERE dest_key=? LIMIT 1", (k,)):
                continue
            it.dest, it.dest_key = cand, k
            taken.add(k)
            return True
        return False

    def _apply_policy(self, it: PlanItem, other: str, taken: set[str]) -> None:
        policy = self.p.get("conflict_policy", "review")
        it.flags.append(Flag.CONFLICT.value)
        it.conflict = f"“{os.path.basename(it.dest)}” would collide with {other}."
        if policy == "alternate" and self._alternate(it, taken):
            it.reason += f" Renamed to “{os.path.basename(it.dest)}” to avoid the collision."
            return
        if policy == "skip":
            it.action = Action.SKIP.value
            it.reason = "Skipped because of a name collision (your conflict setting)."
        else:
            it.action = Action.REVIEW.value
            it.reason = "Name collision — choose: skip, rename with a number, or exclude the other file."
        it.dest = None if it.action != Action.MOVE.value else it.dest
        if it.action != Action.MOVE.value:
            it.dest_key = None

    def _resolve_plan_collisions(self) -> None:
        store = self.store
        keys = [r[0] for r in store.rows(
            "SELECT dest_key FROM items WHERE dest_key IS NOT NULL AND action IN ('move','none') "
            "GROUP BY dest_key HAVING COUNT(*)>1 ORDER BY dest_key")]
        for k in keys:
            self._check_cancel()
            group = sorted(store.query("dest_key=? AND action IN ('move','none')", (k,)),
                           key=lambda i: (i.action != Action.NONE.value, i.source))
            winner = group[0]
            taken: set[str] = set()
            changed = []
            for it in group[1:]:
                same, h = self._same_content(winner.source, it.source, winner.size, it.size)
                if same:
                    it.action = Action.SKIP.value
                    it.flags.append(Flag.DUPLICATE.value)
                    it.content_hash = h
                    it.reason = f"Identical copy of {winner.source}; left in place (use Find duplicates to remove)."
                    it.dest, it.dest_key = None, None
                else:
                    self._apply_policy(it, f"{winner.source}", taken)
                changed.append(it)
            if any(Flag.CONFLICT.value in c.flags for c in changed):
                winner.flags.append(Flag.CONFLICT.value)
                winner.conflict = winner.conflict or "Another file wanted the same name; this one keeps it."
                changed.append(winner)
            store.update_many(changed)

    def _resolve_existing_collisions(self) -> None:
        store = self.store
        changed: list[PlanItem] = []
        for it in store.query("action='move'"):
            self._check_cancel()
            d, n = os.path.split(it.dest)
            root = it.dest_root or d
            lst = self.listing.listing(d, lambda x: self.key(x, root))
            if not lst:
                continue
            k = self.key(n, root)
            if k not in lst:
                continue
            existing = os.path.join(d, lst[k])
            try:
                if os.path.samefile(existing, it.source):
                    if lst[k] == n:
                        it.action = Action.NONE.value
                        it.reason = "Already organized: " + it.reason
                    else:
                        it.reason += " (Case/Unicode-only rename; done through a temporary name.)"
                        it.payload["case_only"] = True
                    changed.append(it)
                    continue
            except OSError:
                pass
            mover = store.scalar("SELECT action FROM items WHERE source=?", (existing,))
            if mover == Action.MOVE.value:
                continue  # occupant leaves in the same batch; executor stages the swap
            if os.path.isdir(existing):
                it.flags.append(Flag.ERROR.value)
                it.action = Action.REVIEW.value
                it.conflict = f"A folder named “{lst[k]}” already exists at the destination."
                it.dest = it.dest_key = None
                changed.append(it)
                continue
            try:
                esz = os.path.getsize(existing)
            except OSError:
                esz = -1
            same, h = self._same_content(it.source, existing, it.size, esz)
            if same:
                it.action = Action.SKIP.value
                it.flags.append(Flag.DUPLICATE.value)
                it.content_hash = h
                it.reason = f"An identical file already exists at {existing}; left in place."
                it.dest = it.dest_key = None
            else:
                self._apply_policy(it, f"the existing file “{existing}”", set())
            changed.append(it)
        if changed:
            store.update_many(changed)

    def _propagate_companions(self) -> None:
        """Keep companions with their primaries: a blocked companion holds its video back,
        and a held-back primary keeps its companions in place."""
        store = self.store
        for _ in range(2):
            changed: list[PlanItem] = []
            # companions in conflict -> primary to review (video companions only)
            for it in store.query("role='companion' AND flags LIKE '%conflict%' AND action IN ('review','skip')"):
                parent = store.get(it.parent_id) if it.parent_id else None
                if parent and parent.category in ("tv", "movies") and parent.action == Action.MOVE.value:
                    parent.action = Action.REVIEW.value
                    parent.flags.append(Flag.CONFLICT.value)
                    parent.reason = f"Held back because its companion “{os.path.basename(it.source)}” has a name collision."
                    parent.dest = parent.dest_key = None
                    changed.append(parent)
            store.update_many(changed)
            changed = []
            for it in store.query("role='companion' AND action IN ('move','rewrite','write_tags')"):
                parent = store.get(it.parent_id) if it.parent_id else None
                if parent and parent.action not in (Action.MOVE.value, Action.NONE.value) and \
                        (parent.category in ("tv", "movies") or it.action != Action.MOVE.value):
                    it.action = Action.REVIEW.value if parent.action == Action.REVIEW.value else Action.SKIP.value
                    it.reason = f"Stays with “{os.path.basename(parent.source)}”, which is not being moved."
                    it.dest = it.dest_key = None
                    changed.append(it)
            store.update_many(changed)
            if not changed:
                break
