"""Embedded audio tag reading via mutagen (read-only) and explicit tag writing.

Reading never modifies files. Every value carries provenance ("tag:<format>").
"""
from __future__ import annotations

import re
from typing import Any

import mutagen
from mutagen.apev2 import APEv2
from mutagen.asf import ASF
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

FIELDS = ("title", "artist", "album", "album_artist", "track", "track_total", "disc",
          "disc_total", "date", "compilation", "genre", "mb_albumid", "release_comment")


def _first(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _text(v: Any) -> str | None:
    v = _first(v)
    if v is None:
        return None
    if hasattr(v, "text"):  # ID3 frame
        v = _first(v.text)
    if hasattr(v, "value"):  # ASF attribute
        v = v.value
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8")
        except UnicodeDecodeError:
            return None
    s = str(v).replace("\x00", " ").strip()
    return s or None


def _num_pair(v: Any) -> tuple[int | None, int | None]:
    """Parse '3', '3/12', (3, 12), ' 03 '."""
    v0 = _first(v)
    if isinstance(v0, tuple):
        n, t = (v0 + (None, None))[:2]
        return (n or None), (t or None)
    s = _text(v)
    if not s:
        return None, None
    m = re.match(r"^\s*(\d+)\s*(?:/\s*(\d+))?", s)
    if not m:
        return None, None
    n = int(m.group(1))
    t = int(m.group(2)) if m.group(2) else None
    return (n or None), t


def _bool(v: Any) -> bool:
    v0 = _first(v)
    if isinstance(v0, bool):
        return v0
    s = _text(v)
    return bool(s) and s.strip().lower() in ("1", "true", "yes")


_ID3_MAP = {"TIT2": "title", "TPE1": "artist", "TALB": "album", "TPE2": "album_artist",
            "TCON": "genre", "TCMP": "compilation"}
_MP4_MAP = {"\xa9nam": "title", "\xa9ART": "artist", "\xa9alb": "album", "aART": "album_artist",
            "\xa9day": "date", "\xa9gen": "genre", "cpil": "compilation"}
_VORBIS_MAP = {"title": "title", "artist": "artist", "album": "album", "albumartist": "album_artist",
               "album artist": "album_artist", "album_artist": "album_artist", "date": "date",
               "year": "date", "originaldate": "original_date", "genre": "genre",
               "compilation": "compilation", "musicbrainz_albumid": "mb_albumid",
               "tracktotal": "track_total", "totaltracks": "track_total",
               "disctotal": "disc_total", "totaldiscs": "disc_total",
               "comment": None, "releasecomment": "release_comment", "version": "release_comment"}
_ASF_MAP = {"Title": "title", "Author": "artist", "WM/AlbumTitle": "album",
            "WM/AlbumArtist": "album_artist", "WM/Year": "date", "WM/Genre": "genre",
            "WM/MusicBrainz/Album Id": "mb_albumid", "MusicBrainz/Album Id": "mb_albumid"}
_APE_MAP = {"title": "title", "artist": "artist", "album": "album", "album artist": "album_artist",
            "albumartist": "album_artist", "year": "date", "genre": "genre",
            "compilation": "compilation"}


def _from_id3(tags: ID3, out: dict) -> None:
    for fid, key in _ID3_MAP.items():
        fr = tags.get(fid)
        if fr is not None:
            out[key] = _bool(fr.text) if key == "compilation" else _text(fr)
    for fid in ("TDRC", "TYER", "TDOR", "TORY"):
        fr = tags.get(fid)
        if fr is not None and _text(fr):
            out.setdefault("date", _text(fr))
    if tags.get("TRCK") is not None:
        out["track"], out["track_total"] = _num_pair(tags["TRCK"].text)
    if tags.get("TPOS") is not None:
        out["disc"], out["disc_total"] = _num_pair(tags["TPOS"].text)
    for fr in tags.getall("TXXX"):
        desc = (fr.desc or "").lower()
        if desc in ("musicbrainz album id",):
            out["mb_albumid"] = _text(fr)
        elif desc in ("albumartist", "album artist") and not out.get("album_artist"):
            out["album_artist"] = _text(fr)
        elif desc in ("releasecomment", "version"):
            out["release_comment"] = _text(fr)


def read_audio(path: str) -> dict[str, Any]:
    """Return normalized tag dict with keys from FIELDS plus format info and `_source`.

    Raises nothing for unsupported files; returns {"_error": ...} instead.
    """
    try:
        f = mutagen.File(path, easy=False)
    except Exception as e:  # mutagen raises many types for corrupt files
        return {"_error": f"Could not read tags: {e.__class__.__name__}"}
    if f is None:
        return {"_error": "No readable tags (format not recognised by mutagen)"}
    out: dict[str, Any] = {}
    fmt = type(f).__name__
    tags = f.tags
    try:
        if isinstance(tags, ID3):
            _from_id3(tags, out)
        elif isinstance(f, MP4) and tags is not None:
            for k, key in _MP4_MAP.items():
                if k in tags:
                    out[key] = _bool(tags[k]) if key == "compilation" else _text(tags[k])
            if "trkn" in tags:
                out["track"], out["track_total"] = _num_pair(tags["trkn"])
            if "disk" in tags:
                out["disc"], out["disc_total"] = _num_pair(tags["disk"])
            for k in tags.keys():
                if k.startswith("----:com.apple.iTunes:MusicBrainz Album Id"):
                    out["mb_albumid"] = _text(tags[k])
        elif isinstance(f, ASF) and tags is not None:
            for k, key in _ASF_MAP.items():
                if k in tags:
                    out[key] = _text(tags[k])
            if "WM/TrackNumber" in tags:
                out["track"], out["track_total"] = _num_pair(tags["WM/TrackNumber"])
            if "WM/PartOfSet" in tags:
                out["disc"], out["disc_total"] = _num_pair(tags["WM/PartOfSet"])
            if "WM/IsCompilation" in tags:
                out["compilation"] = _bool(tags["WM/IsCompilation"])
        elif isinstance(tags, APEv2):
            low = {k.lower(): v for k, v in tags.items()}
            for k, key in _APE_MAP.items():
                if k in low:
                    out[key] = _bool(str(low[k])) if key == "compilation" else _text(str(low[k]))
            if "track" in low:
                out["track"], out["track_total"] = _num_pair(str(low["track"]))
            if "disc" in low:
                out["disc"], out["disc_total"] = _num_pair(str(low["disc"]))
        elif tags is not None and hasattr(tags, "keys"):  # Vorbis comments: FLAC, Ogg, Opus
            low: dict[str, Any] = {}
            for k in tags.keys():
                low.setdefault(k.lower(), tags[k])
            for k, key in _VORBIS_MAP.items():
                if key and k in low and not out.get(key):
                    if key == "compilation":
                        out[key] = _bool(low[k])
                    elif key in ("track_total", "disc_total"):
                        out[key] = _num_pair(low[k])[0]
                    else:
                        out[key] = _text(low[k])
            if "tracknumber" in low:
                n, t = _num_pair(low["tracknumber"])
                out["track"] = n
                if t and not out.get("track_total"):
                    out["track_total"] = t
            if "discnumber" in low:
                n, t = _num_pair(low["discnumber"])
                out["disc"] = n
                if t and not out.get("disc_total"):
                    out["disc_total"] = t
    except Exception as e:  # defensive: never let one malformed tag abort a scan
        out["_error"] = f"Tag parse problem: {e.__class__.__name__}"
    info = getattr(f, "info", None)
    codec = None
    if info is not None:
        codec = getattr(info, "codec", None)
        out["length"] = round(float(getattr(info, "length", 0) or 0), 2)
        out["bitrate"] = getattr(info, "bitrate", None)
    fmt_name = {"MP3": "MP3", "FLAC": "FLAC", "OggVorbis": "Vorbis", "OggOpus": "Opus",
                "OggFLAC": "FLAC", "WAVE": "WAV", "AIFF": "AIFF", "ASF": "WMA",
                "MonkeysAudio": "APE", "WavPack": "WavPack", "Musepack": "MPC",
                "DSF": "DSF", "DSDIFF": "DFF"}.get(fmt, fmt)
    if fmt in ("MP4", "EasyMP4"):
        fmt_name = "ALAC" if codec == "alac" else "AAC"
    out["format"] = fmt_name
    out["has_tags"] = tags is not None and any(out.get(k) for k in ("title", "artist", "album"))
    for k in list(out):
        if out[k] in ("", None):
            del out[k]
    out["_source"] = f"tag:{fmt_name}"
    return out


FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(feat\.?|ft\.?|featuring)\s+.+$", re.IGNORECASE)


def primary_artist(artist: str | None) -> str | None:
    """'A feat. B' -> 'A'. Featured artists stay in the track's own tag untouched."""
    if not artist:
        return artist
    s = FEAT_RE.sub("", artist).strip()
    return s or artist


def year_of(date: str | None) -> int | None:
    if not date:
        return None
    m = re.match(r"^\s*(\d{4})", str(date))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- tag writing (explicit only)

WRITABLE = {"title", "artist", "album", "album_artist", "date", "genre", "track", "disc"}


def write_audio_tags(path: str, values: dict[str, Any]) -> None:
    """Write a small set of fields. Only called for plans with tag writing explicitly enabled."""
    unknown = set(values) - WRITABLE
    if unknown:
        raise ValueError(f"Cannot write fields: {sorted(unknown)}")
    f = mutagen.File(path, easy=True)
    if f is None:
        raise ValueError("Format does not support tag writing")
    if f.tags is None:
        f.add_tags()
    keymap = {"album_artist": "albumartist", "track": "tracknumber", "disc": "discnumber"}
    for k, v in values.items():
        key = keymap.get(k, k)
        if v is None:
            if key in f.tags:
                del f.tags[key]
        else:
            f.tags[key] = [str(v)]
    f.save()
