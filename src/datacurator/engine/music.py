"""Music library browsing and previewed filename-to-metadata suggestions."""
from __future__ import annotations

import os
import re

from ..metadata.audio import year_of
from ..models import CuratorError, Kind
from .planner import norm, strip_disc
from .scanner import normalize_roots

EDIT_FIELDS = {"title": "Title", "artist": "Artist", "album_artist": "Album artist",
               "album": "Album", "genre": "Genre", "year": "Year", "track": "Track", "disc": "Disc"}
FILENAME_MASKS = (
    "{album_artist}/{album}/{track} - {title}",
    "{artist} - {album} - {track} - {title}",
    "{track} - {artist} - {title}",
    "{artist} - {title}",
    "{track} - {title}",
)


def validate_details(values):
    """Validate the whole batch before persisting any changes."""
    out = {}
    for field, value in values.items():
        if field not in EDIT_FIELDS:
            raise CuratorError(f"Unknown music field: {field}")
        value = str(value).strip()
        if not value:
            raise CuratorError(f"{EDIT_FIELDS[field]} cannot be empty. Uncheck it to leave it unchanged.")
        if field in ("year", "track", "disc"):
            if not value.isdigit() or not 1 <= int(value) <= (9999 if field == "year" else 999):
                raise CuratorError(f"{EDIT_FIELDS[field]} must be a positive number" +
                                   (" of at most four digits." if field == "year" else " below 1000."))
            value = str(int(value))
        out[field] = value
    return out


def _tags(meta, overrides):
    meta = dict(meta or {})
    for field, value in overrides.items():
        if field in ("track", "disc"):
            try:
                meta[field] = int(value)
            except ValueError:
                continue
        elif field == "year":
            meta["date"] = value
        else:
            meta[field] = value
    album, disc = strip_disc(meta.get("album"))
    meta["album"] = album
    if disc and not meta.get("disc"):
        meta["disc"] = disc
    return meta


def library_rows(catalog, sources, cancel=None):
    """Snapshot the current sources with organizing overrides, never reading audio bytes."""
    roots = normalize_roots(sources)
    overrides = catalog.overrides()
    rows = []
    for _, records in catalog.iter_dirs(roots):
        if cancel and cancel.is_set():
            return None
        for record in records:
            if record.kind != Kind.AUDIO.value or not any(
                    record.path.startswith(root.rstrip(os.sep) + os.sep) for root in roots):
                continue
            own = overrides.get(record.path, {})
            pre = _tags(record.meta, own)
            group = f"group:music:{os.path.dirname(record.path)}|{norm(pre.get('album'))}"
            merged = {**overrides.get(group, {}), **own}
            meta = _tags(record.meta, merged)
            row = {field: meta.get(field) or "" for field in EDIT_FIELDS}
            row.update(path=record.path, root=record.root, year=year_of(meta.get("date")) or "",
                       format=meta.get("format") or os.path.splitext(record.path)[1].lstrip(".").upper(),
                       error=record.meta_error or "", edited=bool(merged))
            row["missing"] = not all(row.get(k) for k in ("title", "album", "track")) or not (
                row["album_artist"] or row["artist"])
            rows.append(row)
    return sorted(rows, key=lambda r: (str(r["album_artist"] or r["artist"]).casefold(),
                                      str(r["album"]).casefold(), r["disc"] or 0, r["track"] or 0, r["path"]))


class FilenameMask:
    """Extract fields from an exact filename/relative-path pattern; never guess matches."""
    def __init__(self, mask):
        mask = mask.strip().replace("\\", "/")
        if not mask or mask.startswith("/") or ".." in mask.split("/"):
            raise CuratorError("Use a filename or a path relative to the source folder.")
        parts, fields, pos = [], [], 0
        for token in re.finditer(r"\{([a-z_]+)(?::0?\d+)?\}", mask):
            literal = mask[pos:token.start()]
            if "{" in literal or "}" in literal:
                raise CuratorError("Use fields such as {artist}, {album}, {track} and {title}.")
            field = token[1]
            if field not in EDIT_FIELDS or field in fields:
                raise CuratorError(f"Unknown or repeated music field: {field}")
            if pos and not literal:
                raise CuratorError("Separate adjacent fields with text, for example {artist} - {title}.")
            parts.append(re.escape(literal))
            parts.append(f"(?P<{field}>" + (r"\d{1,4}" if field == "year" else
                                           r"\d{1,3}" if field in ("track", "disc") else r"[^/]+?") + ")")
            fields.append(field)
            pos = token.end()
        tail = mask[pos:]
        if not fields or "{" in tail or "}" in tail:
            raise CuratorError("Use fields such as {artist}, {album}, {track} and {title}.")
        parts.append(re.escape(tail))
        self.relative = "/" in mask
        self.regex = re.compile("^" + "".join(parts) + "$")

    def suggest(self, row, missing_only=True):
        path = os.path.relpath(row["path"], row["root"]) if self.relative else os.path.basename(row["path"])
        stem = os.path.splitext(path)[0].replace(os.sep, "/")
        match = self.regex.fullmatch(stem)
        if not match:
            return {}
        values = validate_details(match.groupdict())
        return {field: value for field, value in values.items()
                if (not missing_only or not row.get(field)) and str(row.get(field, "")) != value}


def save_details(catalog, changes):
    """Atomically save reviewed edits to the catalog; application happens in a new plan."""
    checked = {path: validate_details(values) for path, values in changes.items()}
    with catalog.tx() as db:
        for path, values in checked.items():
            if not db.execute("SELECT 1 FROM files WHERE path=? AND kind=?", (path, Kind.AUDIO.value)).fetchone():
                raise CuratorError(f"Track is no longer in the library: {path}. Scan again.")
            for field, value in values.items():
                db.execute("INSERT OR REPLACE INTO overrides VALUES(?,?,?)", (path, field, value))
    return sum(len(values) for values in checked.values())
