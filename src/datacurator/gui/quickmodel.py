"""Qt-free logic behind the Quick organize page (unit-testable without PySide6)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class Group:
    name: str
    files: int = 0
    size: int = 0
    children: dict[str, "Group"] = field(default_factory=dict)

    def child(self, name: str) -> "Group":
        return self.children.setdefault(name, Group(name))

    def sorted_children(self) -> list["Group"]:
        return sorted(self.children.values(), key=lambda g: (-g.files, g.name.lower()))


def _owning_source(path: str, sources: list[str]) -> str | None:
    best = None
    for s in sources:
        s = s.rstrip(os.sep)
        if (path == s or path.startswith(s + os.sep)) and (best is None or len(s) > len(best)):
            best = s
    return best


def group_destinations(rows: Iterable[tuple[str, int, int]], sources: list[str]) -> list[Group]:
    """Summarize ``(dest_dir, file_count, bytes)`` rows as top-level destination folders.

    Level 1 is the first folder under the owning source (``Documents``, ``Other files`` …);
    level 2 is the next folder (``Archives``, ``2026`` …). With several sources the source's
    name prefixes level 1 so folders from different sources are not merged.
    """
    top: dict[str, Group] = {}
    multi = len(sources) > 1
    for dest_dir, count, size in rows:
        if not dest_dir:
            continue
        src = _owning_source(dest_dir, sources)
        rel = os.path.relpath(dest_dir, src) if src else os.path.basename(dest_dir)
        parts = [] if rel == "." else [p for p in rel.split(os.sep) if p]
        name = parts[0] if parts else "(directly in the source folder)"
        if multi and src:
            name = f"{os.path.basename(src)} › {name}"
        g = top.setdefault(name, Group(name))
        g.files += count
        g.size += size
        if len(parts) > 1:
            c = g.child(parts[1])
            c.files += count
            c.size += size
    return sorted(top.values(), key=lambda g: (-g.files, g.name.lower()))


def sample_moves(rows: Iterable[tuple[str, str]], sources: list[str], limit: int = 8) -> list[tuple[str, str]]:
    """``(source, dest)`` pairs shortened relative to their source folder for display."""
    out = []
    for src_path, dest in rows:
        owner = _owning_source(src_path, sources)
        base = owner or os.path.dirname(src_path)
        out.append((os.path.relpath(src_path, base), os.path.relpath(dest, base) if dest.startswith(base) else dest))
        if len(out) >= limit:
            break
    return out


def suggested_folders(home: str) -> list[str]:
    """Common messy folders that exist under `home`, in the order people tend to need them."""
    return [p for p in (os.path.join(home, n) for n in ("Downloads", "Desktop", "Documents", "Pictures",
                                                         "Music", "Movies")) if os.path.isdir(p)]


def explain_scan_notice(files: int, errors: int, protected: int) -> str:
    """One calm sentence about what the scan could not do, or '' when everything was read."""
    if files == 0 and not errors:
        return "No files were found in the chosen folders."
    if errors:
        return (f"{errors:,} folder(s) could not be read, so this suggestion is incomplete. "
                "Open Advanced steps → Review → Scan details for the list.")
    if protected:
        return (f"{protected:,} folder(s) (apps, projects, libraries, games) stay exactly as they are "
                "because their files depend on where they are.")
    return ""
