"""Files that only work together, and files that must stay where they are.

*File sets* are groups in one folder that break when separated: multi-part
archives, disc images and their tracks, GIS shapefiles, photos with their edit
sidecars. One file leads the set; the others follow it wherever it goes and are
held back with it.

*Anchored files* stay in place because other things find them by location:
hidden settings files, project files that open nearby media by relative path,
and saved web pages next to their ``_files`` folder.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from ..models import AUDIO_EXT, IMAGE_EXT, VIDEO_EXT
from .exclusions import PROJECT_FILE_EXT, SAVED_PAGE_SUFFIXES

RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf"}
PHOTO_SIDECARS = (".xmp", ".aae")
SHAPEFILE_PARTS = (".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".fix", ".shp.xml", ".qmd", ".atx")
# Self-contained project formats are organized normally; these reference outside media.
SELF_CONTAINED = {".tex", ".ipynb"}
ANCHORED_PROJECT_EXT = {e: why for e, why in PROJECT_FILE_EXT.items() if e not in SELF_CONTAINED}

INCOMPLETE_DOWNLOAD_EXT = {".crdownload", ".part", ".partial", ".download", ".opdownload", ".!qb", ".!ut", ".aria2"}

_PART_RAR = re.compile(r"^(?P<base>.+)\.part0*(?P<n>\d+)\.rar$", re.I)
_OLD_RAR = re.compile(r"^(?P<base>.+)\.(?:r\d{2,3}|s\d{2})$", re.I)
_NUMBERED = re.compile(r"^(?P<base>.+)\.(?P<n>\d{3})$")
_ZIP_SPLIT = re.compile(r"^(?P<base>.+)\.z\d{2}$", re.I)
_CUE_FILE = re.compile(r'^\s*FILE\s+(?:"([^"]+)"|(\S+))', re.I | re.M)


@dataclass(frozen=True, slots=True)
class Member:
    leader: str        # path of the file the set follows
    why: str           # plain-language reason shown in the review
    renamable: bool    # may the member take the leader's new name prefix


def _read_small(path: str, limit: int = 256 * 1024) -> str:
    try:
        with open(path, "rb") as fh:
            data = fh.read(limit)
    except OSError:
        return ""
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return ""


def find_sets(paths: list[str]) -> dict[str, Member]:
    """Map each set member (not the leader) in one folder to its leader."""
    by_low = {os.path.basename(p).casefold(): p for p in sorted(paths)}
    out: dict[str, Member] = {}

    def bind(leader: str, member_low: str, why: str, renamable: bool = True):
        member = by_low.get(member_low)
        if member and member != leader and member not in out and leader not in out:
            ext = os.path.splitext(member_low)[1]
            if ext in AUDIO_EXT or ext in VIDEO_EXT:
                return  # media keep their own (richer) rules
            out[member] = Member(leader, why, renamable)

    names = sorted(by_low)
    # multi-part archives ------------------------------------------------------------
    parts: dict[str, list[tuple[int, str]]] = {}
    for n in names:
        if m := _PART_RAR.match(n):
            parts.setdefault(m["base"] + ".partrar", []).append((int(m["n"]), n))
    for key, group in parts.items():
        if len(group) > 1:
            group.sort()
            for _, n in group[1:]:
                bind(by_low[group[0][1]], n, "Part of a multi-part archive; stays with the first part.")
    for n in names:
        if n.endswith(".rar") and not _PART_RAR.match(n):
            base = n[:-4]
            for other in names:
                if (m := _OLD_RAR.match(other)) and m["base"] == base:
                    bind(by_low[n], other, "Part of a multi-part archive; stays with the first part.")
        if n.endswith(".zip"):
            base = n[:-4]
            for other in names:
                if (m := _ZIP_SPLIT.match(other)) and m["base"] == base:
                    bind(by_low[n], other, "Part of a split zip archive; stays with the .zip file.")
    numbered: dict[str, list[tuple[int, str]]] = {}
    for n in names:
        if m := _NUMBERED.match(n):
            numbered.setdefault(m["base"], []).append((int(m["n"]), n))
    for base, group in numbered.items():
        if len(group) > 1:
            group.sort()
            for _, n in group[1:]:
                bind(by_low[group[0][1]], n, "Part of a split file; stays with the first part.")
    # disc images ----------------------------------------------------------------------
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext in (".cue", ".gdi"):
            text = _read_small(by_low[n])
            refs = ([a or b for a, b in _CUE_FILE.findall(text)] if ext == ".cue" else
                    [tok.strip('"') for line in text.splitlines()[1:] for tok in line.split()
                     if "." in tok.strip('"')])
            refs = [os.path.basename(r.replace("\\", "/")).casefold() for r in refs]
            if any(os.path.splitext(r)[1] in AUDIO_EXT for r in refs):
                continue  # an album cue sheet: the music rules keep it with its tracks
            for r in refs:
                bind(by_low[n], r, "Disc image track; the cue sheet finds it by name.", renamable=False)
        elif ext == ".ccd":
            for e in (".img", ".sub"):
                bind(by_low[n], stem + e, "Part of a CloneCD disc image.")
        elif ext == ".mds":
            bind(by_low[n], stem + ".mdf", "Part of an Alcohol disc image.")
        elif ext == ".shp":
            for e in SHAPEFILE_PARTS:
                bind(by_low[n], stem + e, "Part of a shapefile; GIS apps need every part together.")
    # photo sidecars ----------------------------------------------------------------------
    images: dict[str, list[str]] = {}
    for n in names:
        stem, ext = os.path.splitext(n)
        if ext in IMAGE_EXT:
            images.setdefault(stem, []).append(n)
    for stem, group in images.items():
        leader = sorted(group, key=lambda n: (os.path.splitext(n)[1] not in RAW_EXT, n))[0]
        for side in PHOTO_SIDECARS:
            bind(by_low[leader], stem + side, "Photo edit sidecar; stays with its photo.")
            for g in group:
                bind(by_low[leader], g + side, "Photo edit sidecar; stays with its photo.")
    return out


def anchored_reason(path: str, siblings_low: set[str]) -> str | None:
    """Why this file must stay where it is, or None."""
    name = os.path.basename(path)
    low = name.casefold()
    stem, ext = os.path.splitext(low)
    if name.startswith("."):
        return "Hidden settings file: apps look for it in this exact place."
    if ext in INCOMPLETE_DOWNLOAD_EXT:
        return "Download still in progress (or interrupted): the app downloading it expects it here."
    if ext in ANCHORED_PROJECT_EXT:
        return (f"{ANCHORED_PROJECT_EXT[ext]}: it opens media from nearby folders by relative path, "
                "so it stays put.")
    if ext in (".html", ".htm", ".xhtml", ".shtml") and any(stem + s in siblings_low for s in SAVED_PAGE_SUFFIXES):
        return "Saved web page: its images and styles live in the folder beside it."
    return None


def follow_name(member: str, leader_src: str, leader_dest: str) -> str:
    """The member's new name when its leader is renamed (shared prefix replaced)."""
    name, old, new = os.path.basename(member), os.path.basename(leader_src), os.path.basename(leader_dest)
    if old == new:
        return name
    i = 0
    while i < min(len(old), len(new)) and old[-1 - i] == new[-1 - i]:
        i += 1
    old_prefix, new_prefix = old[:len(old) - i], new[:len(new) - i]
    if name.startswith(old_prefix):
        return new_prefix + name[len(old_prefix):]
    return name
