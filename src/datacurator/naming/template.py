"""Path templates.

Syntax
------
``{token}``            value of *token*
``{token:02}``         zero-padded integer (any width)
``{a|b}``              first non-empty of a, b (fallback chain)
``[ ... ]``            optional section: rendered only if every token inside has a value
``[[`` / ``]]``        literal brackets
``/``                  separates folder levels (folder templates only)

Missing values never leave stray punctuation behind: after rendering, each path
component is cleaned of empty brackets, doubled separators and dangling
leading/trailing separators.

A folder component that becomes empty is dropped only when it consisted solely of
optional sections (e.g. ``[Disc {disc:02}]``). If a component with a *required*
token renders empty, rendering reports the missing tokens so the planner can
send the file to review instead of silently flattening the hierarchy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from ..models import TemplateError

MUSIC_TOKENS = {"album_artist", "artist", "album", "year", "disc", "disc_number", "disc_total",
                "track", "track_total", "title", "genre", "format", "ext", "extension",
                "original_name", "release"}
TV_TOKENS = {"series", "series_year", "season", "episode", "episode_code", "episode_title",
             "absolute", "air_date", "resolution", "source", "codec", "ext", "extension",
             "original_name"}
MOVIE_TOKENS = {"title", "year", "edition", "resolution", "source", "codec", "ext", "extension",
                "original_name"}
DOCUMENT_TOKENS = {"project", "subpath", "name", "year", "ext", "extension", "original_name"}
PHOTO_TOKENS = {"year", "month", "day", "camera_make", "camera_model", "name", "ext",
                "extension", "original_name"}
OTHER_TOKENS = {"name", "ext", "extension", "original_name", "type", "group", "project", "subpath"}

TOKENS_BY_CATEGORY = {
    "music": MUSIC_TOKENS, "tv": TV_TOKENS, "movies": MOVIE_TOKENS,
    "documents": DOCUMENT_TOKENS, "photos": PHOTO_TOKENS, "other": OTHER_TOKENS,
}

_TOKEN_RE = re.compile(r"\{([a-z_|]+)(?::(0?\d+))?\}")


@dataclass(frozen=True, slots=True)
class _Lit:
    text: str


@dataclass(frozen=True, slots=True)
class _Tok:
    names: tuple[str, ...]
    width: int | None


@dataclass(frozen=True, slots=True)
class _Opt:
    parts: tuple


def _parse(s: str) -> tuple:
    """Parse into a tuple of _Lit/_Tok/_Opt nodes. Optional sections may nest."""
    out: list = []
    stack: list[list] = [out]
    i = 0
    buf = ""

    def flush():
        nonlocal buf
        if buf:
            stack[-1].append(_Lit(buf))
            buf = ""

    while i < len(s):
        c = s[i]
        if s.startswith("[[", i):
            buf += "["
            i += 2
            continue
        if s.startswith("]]", i):
            buf += "]"
            i += 2
            continue
        if c == "[":
            flush()
            stack.append([])
            i += 1
            continue
        if c == "]":
            flush()
            if len(stack) == 1:
                raise TemplateError(f"Unbalanced ']' at position {i} in template {s!r}")
            parts = stack.pop()
            stack[-1].append(_Opt(tuple(parts)))
            i += 1
            continue
        if c == "{":
            m = _TOKEN_RE.match(s, i)
            if not m:
                raise TemplateError(f"Malformed token at position {i} in template {s!r}")
            flush()
            names = tuple(n for n in m.group(1).split("|") if n)
            if not names:
                raise TemplateError(f"Empty token at position {i} in template {s!r}")
            width = int(m.group(2)) if m.group(2) else None
            stack[-1].append(_Tok(names, width))
            i = m.end()
            continue
        if c == "}":
            raise TemplateError(f"Unbalanced '}}' at position {i} in template {s!r}")
        buf += c
        i += 1
    flush()
    if len(stack) != 1:
        raise TemplateError(f"Unclosed '[' in template {s!r}")
    return tuple(out)


def _tokens_in(nodes) -> set[str]:
    found: set[str] = set()
    for n in nodes:
        if isinstance(n, _Tok):
            found.update(n.names)
        elif isinstance(n, _Opt):
            found |= _tokens_in(n.parts)
    return found


def _has_required(nodes) -> bool:
    return any(isinstance(n, _Tok) for n in nodes)


def _value(tok: _Tok, values: Mapping[str, Any]) -> str | None:
    for name in tok.names:
        v = values.get(name)
        if v is None:
            continue
        if isinstance(v, str):
            if v.strip() == "":
                continue
            if tok.width and v.isdigit():
                return v.zfill(tok.width)
            return v
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return str(v).zfill(tok.width) if tok.width else str(v)
        return str(v)
    return None


def _render_nodes(nodes, values, missing: list[str]) -> str | None:
    """Return rendered string; None if this (optional) level must vanish.

    At top level `missing` collects required tokens that had no value.
    """
    out = []
    for n in nodes:
        if isinstance(n, _Lit):
            out.append(n.text)
        elif isinstance(n, _Tok):
            v = _value(n, values)
            if v is None:
                missing.append("|".join(n.names))
                out.append("")
            else:
                out.append(v)
        else:  # optional
            sub_missing: list[str] = []
            r = _render_nodes(n.parts, values, sub_missing)
            if not sub_missing and r is not None:
                out.append(r)
    return "".join(out)


_EMPTY_BRACKETS = re.compile(r"\(\s*\)|\[\s*\]|\{\s*\}")
# " - - " style runs left by a missing value between two separators
_SEP_RUN = re.compile(r"(?:\s+[-–—,]\s*|\s*[-–—,]\s+)(?:[-–—,]\s*)+")
_LEAD = re.compile(r"^\s*[-–—,_]+\s+|^\s*[-–—,_]+$")
_TRAIL = re.compile(r"\s+[-–—,_]+\s*$|^[-–—,_]+\s*$")


def clean_component(s: str) -> str:
    """Remove punctuation left around missing values.

    Only separator *runs* and dangling separators bounded by whitespace are
    touched, so values like "Jay-Z", "R.E.M." or "Wait..." survive unchanged.
    """
    prev = None
    while prev != s:
        prev = s
        s = _EMPTY_BRACKETS.sub("", s)
        s = _SEP_RUN.sub(lambda m: " - " if re.search(r"[-–—]", m.group(0)) else ", ", s)
        s = re.sub(r"\s{2,}", " ", s)
        s = _LEAD.sub("", s)
        s = _TRAIL.sub("", s)
        s = s.strip()
    return s


class Template:
    """A compiled folder or filename template."""

    def __init__(self, source: str, kind: str, category: str):
        self.source = source
        self.kind = kind  # "folder" | "filename"
        self.category = category
        if kind == "filename" and "/" in source:
            raise TemplateError("Filename templates cannot contain '/'. Put folders in the folder template.")
        comps = [c for c in source.split("/")] if kind == "folder" else [source]
        if kind == "folder" and source.strip() == "":
            comps = []
        self._components = [_parse(c) for c in comps]
        allowed = TOKENS_BY_CATEGORY.get(category)
        used = set()
        for c in self._components:
            used |= _tokens_in(c)
        if allowed is not None:
            unknown = sorted(used - allowed)
            if unknown:
                raise TemplateError(
                    f"Unknown token(s) {', '.join('{' + u + '}' for u in unknown)} for {category}. "
                    f"Available: {', '.join(sorted(allowed))}")
        self.tokens = used
        self.ends_with_ext = kind == "filename" and bool(re.search(r"\{(ext|extension)\}\s*$", source))

    def render_folder(self, values: Mapping[str, Any]) -> tuple[list[str], list[str]]:
        """Return (components, missing_required_tokens)."""
        parts: list[str] = []
        missing_all: list[str] = []
        for comp in self._components:
            missing: list[str] = []
            r = _render_nodes(comp, values, missing)
            r = clean_component(r or "")
            if missing:
                missing_all.extend(missing)
            if r == "":
                if _has_required(comp) and not missing:
                    # literal-only component like "Specials" is fine; empty means all optional vanished
                    pass
                if _has_required(comp) and missing:
                    continue  # reported via missing
                continue
            parts.append(r)
        return parts, missing_all

    def render_filename(self, values: Mapping[str, Any], ext: str) -> tuple[str, list[str]]:
        """Return (filename_with_extension, missing_required_tokens). Extension is always preserved."""
        src = self.source
        if self.ends_with_ext:
            src = re.sub(r"\{(ext|extension)\}\s*$", "", src)
        nodes = _parse(src)
        missing: list[str] = []
        stem = clean_component(_render_nodes(nodes, values, missing) or "")
        return stem + ext, missing

    def example(self, values: Mapping[str, Any], ext: str = "") -> str:
        if self.kind == "folder":
            parts, missing = self.render_folder(values)
            s = "/".join(parts)
        else:
            s, missing = self.render_filename(values, ext)
        if missing:
            s += f"   (missing: {', '.join(sorted(set(missing)))})"
        return s


def validate(source: str, kind: str, category: str) -> str | None:
    try:
        Template(source, kind, category)
        return None
    except TemplateError as e:
        return str(e)
