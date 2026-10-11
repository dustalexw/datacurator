"""Filename sanitization and destination-aware name equivalence.

Every change is reported so the preview can show exactly what was altered and why.
"""
from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass, field

from ..storage.capabilities import WIN_FORBIDDEN, WIN_RESERVED, Capabilities

DEFAULT_REPLACEMENTS = {
    "/": "-", "\\": "-", ":": " -", "*": "", "?": "", '"': "'", "<": "(", ">": ")", "|": "-",
}


@dataclass(slots=True)
class NamingRules:
    preset: str = "portable"          # portable | native
    replacements: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_REPLACEMENTS))
    unicode_form: str = "NFC"         # NFC | NFD | none
    max_name: int = 255               # hard cap applied in addition to destination limits

    @classmethod
    def from_dict(cls, d: dict | None) -> "NamingRules":
        d = d or {}
        r = cls()
        r.preset = d.get("preset", r.preset)
        if "replacements" in d:
            r.replacements = {**DEFAULT_REPLACEMENTS, **d["replacements"]}
        r.unicode_form = d.get("unicode_form", r.unicode_form)
        r.max_name = int(d.get("max_name", r.max_name))
        return r


def _windows_rules(rules: NamingRules, caps: Capabilities | None) -> bool:
    return rules.preset == "portable" or (caps is not None and caps.windows_naming)


def _truncate(stem: str, ext: str, max_bytes: int, max_units: int) -> str:
    """Truncate stem at a code-point boundary so stem+ext fits both limits."""
    def fits(s: str) -> bool:
        full = s + ext
        return len(full.encode("utf-8")) <= max_bytes and len(full.encode("utf-16-le")) // 2 <= max_units
    if fits(stem):
        return stem
    lo, hi = 0, len(stem)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if fits(stem[:mid]):
            lo = mid
        else:
            hi = mid - 1
    out = stem[:lo]
    # don't split a combining sequence
    while out and unicodedata.combining(out[-1]):
        out = out[:-1]
    return out.rstrip(" .")


def sanitize_component(name: str, rules: NamingRules, caps: Capabilities | None,
                       is_filename: bool, ext: str = "") -> tuple[str, list[str]]:
    """Return (safe_name, reasons). For filenames, `name` excludes `ext`."""
    reasons: list[str] = []
    s = name
    if rules.unicode_form in ("NFC", "NFD"):
        n = unicodedata.normalize(rules.unicode_form, s)
        if n != s:
            reasons.append(f"Unicode normalized to {rules.unicode_form}")
            s = n
    ctrl = "".join(ch for ch in s if ord(ch) < 32 or ord(ch) == 127)
    if ctrl:
        s = "".join(ch for ch in s if not (ord(ch) < 32 or ord(ch) == 127))
        reasons.append("removed control characters")
    win = _windows_rules(rules, caps)
    forbidden = WIN_FORBIDDEN if win else "/"
    if os.sep == "\\":
        forbidden = WIN_FORBIDDEN
    bad = sorted({ch for ch in s if ch in forbidden})
    if bad:
        for ch in bad:
            s = s.replace(ch, rules.replacements.get(ch, "_"))
        reasons.append("replaced characters not allowed here: " + " ".join(bad))
        s = " ".join(s.split())
    if win:
        stripped = s.rstrip(" .")
        if stripped != s and stripped:
            reasons.append("removed trailing space/period (not allowed on Windows filesystems)")
            s = stripped
        base = s.split(".")[0].strip().upper()
        if base in WIN_RESERVED:
            s = s + "_" if not ext else s + "_"
            reasons.append(f"'{base}' is a reserved device name on Windows")
    lead = s.lstrip(" ")
    if lead != s:
        s = lead
        reasons.append("removed leading spaces")
    if s in ("", ".", ".."):
        s = "_"
        reasons.append("name was empty after cleaning")
    max_bytes = min(rules.max_name, caps.max_name_bytes if caps else 255)
    max_units = min(rules.max_name, caps.max_name_units if caps else 255)
    t = _truncate(s, ext if is_filename else "", max_bytes, max_units)
    if t != s:
        reasons.append(f"shortened to fit the {max_bytes}-byte name limit")
        s = t or "_"
    return s, reasons


def equivalence_key(path: str, caps: Capabilities | None, rules: NamingRules | None = None) -> str:
    """Key under which two paths name the same entry on the destination.

    Portable naming treats case and Unicode-normalization variants as the same so
    a library stays valid when copied to another OS.
    """
    s = unicodedata.normalize("NFC", path)
    portable = rules is None or rules.preset == "portable"
    if portable or caps is None or not caps.case_sensitive:
        s = s.casefold()
        s = unicodedata.normalize("NFC", s)
    if os.sep == "\\":
        s = s.replace("\\", "/")
    return s
