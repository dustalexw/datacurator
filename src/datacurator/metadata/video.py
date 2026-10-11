"""Filename parsing for TV episodes and movies (offline, deterministic).

Returns only what is present in the name or its folders; nothing is guessed.
"""
from __future__ import annotations

import re
import os
from typing import Any


def read_video(path: str) -> dict[str, Any]:
    """Read local MP4/M4V tags without modifying or decoding video streams."""
    if os.path.splitext(path)[1].lower() not in (".mp4", ".m4v", ".mov"):
        return {}
    try:
        from mutagen.mp4 import MP4
        tags = MP4(path).tags or {}
        def first(key):
            value = tags.get(key)
            return value[0] if isinstance(value, list) and value else value
        out = {"_source": "embedded MP4 tags"}
        title, series = first("\xa9nam"), first("tvsh")
        if series:
            out["series"] = str(series)
            if title:
                out["episode_title"] = str(title)
            for key, field in (("tvsn", "season"), ("tves", "episodes")):
                value = first(key)
                if value is not None and str(value).isdigit():
                    out[field] = [int(value)] if field == "episodes" else int(value)
        elif title:
            out["title"] = str(title)
        date = str(first("\xa9day") or "")
        if re.match(r"^\d{4}", date):
            out["series_year" if series else "year"] = int(date[:4])
        if plot := first("ldes") or first("desc"):
            out["plot"] = str(plot)
        if genre := first("\xa9gen"):
            out["genres"] = [str(genre)]
        return out
    except Exception:
        # Missing/unsupported tags do not invalidate filename or sidecar parsing.
        return {}

RES_RE = re.compile(r"(?<![a-z0-9])(2160p|1440p|1080p|1080i|720p|576p|480p|4k|uhd)(?![a-z0-9])", re.I)
SRC_RE = re.compile(r"(?<![a-z0-9])(web[- .]?dl|webrip|web|blu[- .]?ray|bdrip|brrip|bdremux|remux|hdtv|"
                    r"dvdrip|dvd|hdrip|amzn|nf|dsnp|hmax|atvp|pcok)(?![a-z0-9])", re.I)
CODEC_RE = re.compile(r"(?<![a-z0-9])(x264|x265|h[ .]?264|h[ .]?265|hevc|avc|av1|xvid|divx|vp9)(?![a-z0-9])", re.I)
JUNK_RE = re.compile(r"(?<![a-z0-9])(proper|repack|internal|10bit|8bit|hdr10\+?|hdr|dv|dovi|ddp?[ .]?[257][ .][01]|"
                     r"aac[ .]?[257]?[ .]?[01]?|ac3|e-?ac3|dts(-hd)?|truehd|atmos|multi|subbed|dubbed|"
                     r"complete|extended|unrated)(?![a-z0-9])", re.I)
EDITION_RE = re.compile(r"(?<![a-z0-9])(director'?s[ .]cut|extended([ .](cut|edition))?|unrated|theatrical([ .]cut)?|"
                        r"remastered|special[ .]edition|ultimate[ .](cut|edition)|imax)(?![a-z0-9])", re.I)

SXXEYY = re.compile(r"(?<![a-z0-9])s(\d{1,3})[ ._-]?e(\d{1,4})((?:[ ._]?-?[ ._]?e\d{1,4})*)(?:[ ._]?-[ ._]?(\d{1,4})(?![\dp]))?",
                    re.I)
NXNN = re.compile(r"(?<![a-z0-9])(\d{1,2})x(\d{2,3})((?:[x-]\d{2,3})*)(?![a-z0-9])", re.I)
DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-. _](0[1-9]|1[0-2])[-. _](0[1-9]|[12]\d|3[01])(?!\d)")
ABSOLUTE = re.compile(r"(?:^|[ ._]-[ ._]|[ ._]e|[ ._]ep[ ._]?|#)(\d{2,4})(?:v\d)?(?=$|[ ._\[(-])", re.I)
SEASON_DIR = re.compile(r"^(?:season|series|staffel|saison|temporada|s)[ ._-]*(\d{1,4})$", re.I)
SPECIALS_DIR = re.compile(r"^(specials?|extras?)$", re.I)
YEAR_TAIL = re.compile(r"[ ._(\[]+((?:19|20)\d{2})[)\]]?\s*$")
LEADING_GROUP = re.compile(r"^\s*\[[^\]]*\]\s*")


def _clean(s: str) -> str:
    s = LEADING_GROUP.sub("", s)
    if " " not in s.strip() and ("." in s or "_" in s):
        s = re.sub(r"[._]", " ", s)
    else:
        s = s.replace("_", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -._[](){}")


def quality_tokens(s: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if m := RES_RE.search(s):
        v = m.group(1).lower()
        out["resolution"] = {"4k": "2160p", "uhd": "2160p"}.get(v, v)
    if m := SRC_RE.search(s):
        v = re.sub(r"[ .-]", "", m.group(1)).lower()
        out["source"] = {"webdl": "WEB-DL", "webrip": "WEBRip", "web": "WEB", "bluray": "BluRay",
                         "bdrip": "BDRip", "brrip": "BRRip", "bdremux": "Remux", "remux": "Remux",
                         "hdtv": "HDTV", "dvdrip": "DVDRip", "dvd": "DVD", "hdrip": "HDRip"}.get(v, v.upper())
    if m := CODEC_RE.search(s):
        v = m.group(1).lower().replace(" ", "").replace(".", "")
        out["codec"] = {"h264": "H.264", "x264": "x264", "h265": "H.265", "x265": "x265", "hevc": "HEVC",
                        "avc": "AVC", "av1": "AV1", "xvid": "XviD", "divx": "DivX", "vp9": "VP9"}.get(v, v)
    return out


def _strip_quality(s: str) -> str:
    """Cut a title at the first technical token (resolution/source/codec/junk/year-in-brackets)."""
    cut = len(s)
    for rx in (RES_RE, SRC_RE, CODEC_RE, JUNK_RE):
        m = rx.search(s)
        if m:
            cut = min(cut, m.start())
    s = s[:cut]
    s = re.sub(r"-[A-Za-z0-9]+$", "", s) if cut < len(s) else s
    return s


def _title_after(rest: str) -> str | None:
    rest = _strip_quality(rest)
    t = _clean(rest)
    t = t.strip(" -")
    return t or None


def _series_from(prefix: str, parents: list[str]) -> tuple[str | None, int | None, str]:
    name = _clean(prefix)
    year = None
    m = YEAR_TAIL.search(name)
    if m and name[:m.start()].strip():
        year = int(m.group(1))
        name = name[:m.start()].strip(" -.")
    if name:
        return name, year, "filename"
    for p in parents:
        if SEASON_DIR.match(p.strip()) or SPECIALS_DIR.match(p.strip()):
            continue
        pn = _clean(p)
        m = YEAR_TAIL.search(pn)
        if m and pn[:m.start()].strip():
            return pn[:m.start()].strip(" -."), int(m.group(1)), "folder"
        return (pn or None), None, "folder"
    return None, None, "none"


def _episodes_from_tail(first: int, tail: str, range_end: str | None) -> list[int]:
    eps = [first]
    for m in re.finditer(r"(-)?[ ._]?e(\d{1,4})", tail, re.I):
        n = int(m.group(2))
        if m.group(1) and n > eps[-1]:
            eps.extend(range(eps[-1] + 1, n + 1))
        elif n not in eps:
            eps.append(n)
    if range_end:
        n = int(range_end)
        if n > eps[-1] and n - eps[-1] <= 20:
            eps.extend(range(eps[-1] + 1, n + 1))
    return eps


def parse_tv(stem: str, parents: list[str]) -> dict[str, Any] | None:
    """Parse a TV episode. `parents` are folder names nearest first.

    Returns dict with: series, series_year, season, episodes, air_date, absolute,
    episode_title, pattern, plus quality tokens; or None if no episode pattern.
    """
    m = SXXEYY.search(stem)
    if m:
        season = int(m.group(1))
        eps = _episodes_from_tail(int(m.group(2)), m.group(3) or "", m.group(4))
        series, syear, ssrc = _series_from(stem[:m.start()], parents)
        out = {"series": series, "series_year": syear, "season": season, "episodes": eps,
               "episode_title": _title_after(stem[m.end():]), "pattern": "SxxEyy",
               "series_source": ssrc}
        out.update(quality_tokens(stem))
        return out
    m = NXNN.search(stem)
    if m:
        season = int(m.group(1))
        eps = [int(m.group(2))]
        for mm in re.finditer(r"([x-])(\d{2,3})", m.group(3) or ""):
            n = int(mm.group(2))
            if mm.group(1) == "-" and n > eps[-1]:
                eps.extend(range(eps[-1] + 1, n + 1))
            elif n not in eps:
                eps.append(n)
        series, syear, ssrc = _series_from(stem[:m.start()], parents)
        out = {"series": series, "series_year": syear, "season": season, "episodes": eps,
               "episode_title": _title_after(stem[m.end():]), "pattern": "NxNN", "series_source": ssrc}
        out.update(quality_tokens(stem))
        return out
    m = DATE.search(stem)
    if m and stem[:m.start()].strip(" ._-"):
        y, mo, d = m.group(1), m.group(2), m.group(3)
        series, syear, ssrc = _series_from(stem[:m.start()], parents)
        out = {"series": series, "series_year": syear, "season": int(y), "episodes": [],
               "air_date": f"{y}-{mo}-{d}", "episode_title": _title_after(stem[m.end():]),
               "pattern": "date", "series_source": ssrc}
        out.update(quality_tokens(stem))
        return out
    # Absolute numbering is only a *candidate*; the planner requires an explicit convention.
    core = _strip_quality(LEADING_GROUP.sub("", stem))
    m = None
    for mm in ABSOLUTE.finditer(core):
        m = mm
    if m and core[:m.start()].strip(" ._-"):
        n = int(m.group(1))
        if not (1900 <= n <= 2099):
            series, syear, ssrc = _series_from(core[:m.start()], parents)
            # season folder gives an explicit season for "Show - 05" style names
            season = None
            for p in parents[:1]:
                sm = SEASON_DIR.match(p.strip())
                if sm:
                    season = int(sm.group(1))
                elif SPECIALS_DIR.match(p.strip()):
                    season = 0
            out = {"series": series, "series_year": syear, "season": season, "episodes": [],
                   "absolute": n, "episode_title": _title_after(core[m.end():]),
                   "pattern": "absolute", "series_source": ssrc}
            out.update(quality_tokens(stem))
            return out
    return None


def episode_code(info: dict[str, Any]) -> str | None:
    if info.get("air_date") and info.get("pattern") == "date":
        return info["air_date"]
    eps = info.get("episodes") or []
    season = info.get("season")
    if eps and season is not None:
        w = 3 if max(eps) > 99 else 2
        sw = 2 if season < 100 else len(str(season))
        if len(eps) == 1:
            return f"S{season:0{sw}d}E{eps[0]:0{w}d}"
        contiguous = eps == list(range(eps[0], eps[-1] + 1))
        if contiguous:
            return f"S{season:0{sw}d}E{eps[0]:0{w}d}-E{eps[-1]:0{w}d}"
        return f"S{season:0{sw}d}" + "".join(f"E{e:0{w}d}" for e in eps)
    if info.get("absolute") is not None:
        return f"{info['absolute']:03d}"
    return None


MOVIE_RE = re.compile(r"^(?P<title>.+)[ ._]*[(\[]?(?P<year>(?:19|20)\d{2})[)\]]?(?=$|[ ._\[(-])(?P<rest>.*)$")


def parse_movie(stem: str, parents: list[str]) -> dict[str, Any] | None:
    """'Title (Year)' / 'Title.Year.1080p...' ; falls back to a 'Title (Year)' parent folder."""
    def attempt(s: str):
        s2 = LEADING_GROUP.sub("", s)
        m = MOVIE_RE.match(s2)
        if not m:
            return None
        title = _clean(m.group("title"))
        if not title:
            return None
        rest = m.group("rest")
        out = {"title": title, "year": int(m.group("year"))}
        if em := EDITION_RE.search(rest.replace(".", " ")):
            out["edition"] = " ".join(w.capitalize() if w.lower() != "imax" else "IMAX"
                                      for w in re.split(r"[ .]", em.group(1)))
        out.update(quality_tokens(s))
        return out
    r = attempt(stem)
    if r:
        r["title_source"] = "filename"
        return r
    if parents:
        r = attempt(parents[0])
        if r:
            r["title_source"] = "folder"
            r.update(quality_tokens(stem))
            return r
    return None


def movie_search_hint(stem):
    """A query candidate for an explicit online lookup, never a confirmed movie identity."""
    title = _clean(_strip_quality(stem))
    if not title or title.casefold() in {"movie", "video", "sample", "trailer", "film", "home video"}:
        return None
    return title


# ---------------------------------------------------------------- subtitle suffixes

LANG_CODES = {
    "en", "eng", "fr", "fre", "fra", "de", "ger", "deu", "es", "spa", "it", "ita", "pt", "por", "nl", "dut",
    "nld", "sv", "swe", "no", "nor", "nb", "nn", "da", "dan", "fi", "fin", "pl", "pol", "ru", "rus", "uk",
    "ukr", "cs", "cze", "ces", "sk", "slo", "hu", "hun", "ro", "rum", "ron", "el", "gre", "ell", "tr", "tur",
    "ar", "ara", "he", "heb", "hi", "hin", "ja", "jpn", "ko", "kor", "zh", "chi", "zho", "th", "tha", "vi",
    "vie", "id", "ind", "ms", "may", "bg", "bul", "hr", "hrv", "sr", "srp", "sl", "slv", "et", "est", "lv",
    "lav", "lt", "lit", "is", "ice", "isl", "fa", "per", "fas", "ca", "cat", "eu", "baq", "gl", "glg", "und",
    "pt-br", "pt-pt", "zh-hans", "zh-hant", "zh-cn", "zh-tw", "es-419", "es-mx", "fr-ca", "en-us", "en-gb",
}
SUB_FLAGS = {"forced", "sdh", "cc", "hi", "default", "foreign", "commentary"}


def subtitle_suffix(stem: str) -> str:
    """Trailing language/accessibility tokens of a subtitle stem, e.g. '.en.forced'."""
    parts = stem.split(".")
    tail: list[str] = []
    for p in reversed(parts[1:]):
        if p.lower() in LANG_CODES or p.lower() in SUB_FLAGS:
            tail.append(p)
        else:
            break
    return "".join("." + p for p in reversed(tail))
