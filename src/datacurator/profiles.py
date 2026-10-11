"""Organization profiles and template presets.

A profile fully determines planning; the same files + the same profile produce
the same plan (see planner determinism tests).
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

from .models import CuratorError
from .naming.template import validate

PRESETS: dict[str, dict[str, dict[str, str]]] = {
    "music": {
        "music_in_source": {"folder": "Music/{album_artist}/{album}",
                            "filename": "[{disc}-][{track:02} - ]{title}",
                            "label": "Music / Album Artist / Album / 01 - Title"},
        "genre/album_artist/album": {"folder": "{genre}/{album_artist}/{album}",
                                     "filename": "[{disc}-][{track:02} - ]{title}",
                                     "label": "Genre / Album Artist / Album / 01 - Title"},
        "format/album_artist/album": {"folder": "{format}/{album_artist}/{album}",
                                      "filename": "[{disc}-][{track:02} - ]{title}",
                                      "label": "Format / Album Artist / Album / 01 - Title"},
        "album_artist/album": {"folder": "{album_artist}/{album}",
                               "filename": "[{disc}-][{track:02} - ]{title}",
                               "label": "Album Artist / Album / 01 - Title"},
        "artist/album": {"folder": "{artist}/{album}", "filename": "[{disc}-][{track:02} - ]{title}",
                         "label": "Artist / Album / 01 - Title"},
        "album_artist/year-album": {"folder": "{album_artist}/[{year} - ]{album}",
                                    "filename": "[{disc}-][{track:02} - ]{title}",
                                    "label": "Album Artist / Year - Album / 01 - Title"},
        "album_artist/album/disc": {"folder": "{album_artist}/{album}/[Disc {disc:02}]",
                                    "filename": "[{track:02} - ]{title}",
                                    "label": "Album Artist / Album / Disc 01 / 01 - Title"},
    },
    "tv": {
        "series/season": {"folder": "{series}/[Season {season:02}]",
                          "filename": "{series} - {episode_code}[ - {episode_title}]",
                          "label": "Series / Season 01 / Series - S01E01 - Title"},
        "series_year/season": {"folder": "{series}[ ({series_year})]/[Season {season:02}]",
                               "filename": "{series} - {episode_code}",
                               "label": "Series (Year) / Season 01 / Series - S01E01"},
        "series/season/episode": {
            "folder": "{series}/[Season {season:02}]/{series} - {episode_code}[ - {episode_title}]",
            "filename": "{series} - {episode_code}[ - {episode_title}]",
            "label": "Series / Season 01 / Series - S01E01 - Title / Series - S01E01 - Title"},
    },
    "movies": {
        "title_year": {"folder": "{title} ({year})", "filename": "{title} ({year})[ - {edition}]",
                       "label": "Movie Title (Year) / Movie Title (Year)"},
    },
    "documents": {
        "project": {"folder": "[{project}]", "filename": "{name}", "label": "Project / Filename"},
    },
    "photos": {
        "year/year-month": {"folder": "{year}/{year}-{month:02}", "filename": "{name}",
                            "label": "Year / Year-Month / Filename"},
    },
    "other": {
        "flat": {"folder": "", "filename": "{name}", "label": "Filename"},
    },
}

DEFAULT_PRESET = {"music": "album_artist/album", "tv": "series/season", "movies": "title_year",
                  "documents": "project", "photos": "year/year-month", "other": "flat"}

MODES = ("move_and_rename", "move", "rename")
CONFLICT_POLICIES = ("review", "skip", "alternate")


def _category_defaults(cat: str) -> dict[str, Any]:
    p = PRESETS[cat][DEFAULT_PRESET[cat]]
    d: dict[str, Any] = {"enabled": cat in ("music", "tv", "movies"), "root": "",
                         "mode": "move_and_rename", "preset": DEFAULT_PRESET[cat],
                         "folder": p["folder"], "filename": p["filename"]}
    if cat == "music":
        d.update({"compilation_name": "Various Artists", "path_fallback": True})
    if cat == "tv":
        d.update({"numbering": "auto"})
    if cat in ("documents", "other"):
        d["mode"] = "move"
    if cat == "photos":
        d.update({"date_fallback": "none", "mode": "move"})
    return d


DEFAULT_PROFILE: dict[str, Any] = {
    "name": "Default",
    "version": 1,
    "sources": [],
    "categories": {c: _category_defaults(c) for c in PRESETS},
    "naming": {"preset": "portable", "unicode_form": "NFC"},
    "conflict_policy": "review",
    "companions": {"update_references": False},
    "write_tags": False,
    "detect_duplicates": True,
    "preservation": {"block_lossy": False},
    "cleanup_empty_dirs": False,
    "scan": {"follow_symlinks": False, "cross_mounts": False, "include_hidden": False},
    "unattended": {"enabled": False},
    "network": {"max_concurrent_transfers": 1, "retries": 4},
    "providers": {"tv": "none"},
}


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def apply_preset(cat_cfg: dict, cat: str, preset: str) -> dict:
    p = PRESETS[cat].get(preset)
    if not p:
        raise CuratorError(f"Unknown {cat} preset '{preset}'. Choose one of: {', '.join(PRESETS[cat])}")
    cat_cfg = dict(cat_cfg)
    cat_cfg.update({"preset": preset, "folder": p["folder"], "filename": p["filename"]})
    return cat_cfg


def normalize(profile: dict) -> dict:
    """Merge with defaults, expand presets, validate. Raises CuratorError with all problems."""
    # A category that names a preset but no explicit templates takes the preset's templates.
    raw_cats = (profile or {}).get("categories", {}) or {}
    prof = _merge(DEFAULT_PROFILE, profile or {})
    problems: list[str] = []
    for cat, cfg in prof["categories"].items():
        if cat not in PRESETS:
            problems.append(f"Unknown category '{cat}'")
            continue
        raw = raw_cats.get(cat, {})
        if "preset" in raw and "folder" not in raw and "filename" not in raw:
            try:
                prof["categories"][cat] = cfg = apply_preset(cfg, cat, raw["preset"])
            except CuratorError as e:
                problems.append(str(e))
        if cfg.get("mode") not in MODES:
            problems.append(f"{cat}: mode must be one of {', '.join(MODES)}")
        for kind in ("folder", "filename"):
            err = validate(cfg.get(kind, ""), kind, cat)
            if err:
                problems.append(f"{cat} {kind} template: {err}")
        if cfg.get("root"):
            cfg["root"] = os.path.abspath(os.path.expanduser(cfg["root"]))
    if prof.get("conflict_policy") not in CONFLICT_POLICIES:
        problems.append(f"conflict_policy must be one of {', '.join(CONFLICT_POLICIES)}")
    if prof["categories"]["tv"].get("numbering") not in ("auto", "absolute"):
        problems.append("tv.numbering must be 'auto' or 'absolute'")
    prof["sources"] = [os.path.abspath(os.path.expanduser(s)) for s in prof.get("sources", [])]
    if problems:
        raise CuratorError("Profile problems:\n  - " + "\n  - ".join(problems))
    return prof


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return normalize(json.load(fh))


def save(profile: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def check_unattended(profile: dict) -> None:
    """Unattended runs need an explicitly enabled profile with defined conflict handling."""
    u = profile.get("unattended") or {}
    if not u.get("enabled"):
        raise CuratorError("This profile is not enabled for unattended runs. Set "
                           "\"unattended\": {\"enabled\": true, \"conflict_policy\": ...} in the profile.")
    if u.get("conflict_policy") not in ("skip", "alternate"):
        raise CuratorError("Unattended profiles must define unattended.conflict_policy as 'skip' or "
                           "'alternate' (review is not possible without a person).")
