"""Photo EXIF dates and local Kodi-style .nfo files (offline sources)."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any


def read_photo(path: str) -> dict[str, Any]:
    """EXIF capture date / camera. Reads only the header via Pillow's lazy loader."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover
        return {"_error": "Pillow not installed"}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            if not exif:
                return {"_source": "exif", "has_date": False}
            out: dict[str, Any] = {"_source": "exif"}
            date = None
            try:
                sub = exif.get_ifd(0x8769)
                date = sub.get(36867) or sub.get(36868)
            except Exception:
                pass
            date = date or exif.get(306)
            if date:
                m = re.match(r"^(\d{4})[:\-](\d{2})[:\-](\d{2})", str(date).strip())
                if m and m.group(1) != "0000":
                    out["year"], out["month"], out["day"] = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if exif.get(271):
                out["camera_make"] = str(exif.get(271)).strip("\x00 ")
            if exif.get(272):
                out["camera_model"] = str(exif.get(272)).strip("\x00 ")
            out["has_date"] = "year" in out
            return out
    except Exception as e:
        return {"_error": f"Could not read image metadata: {e.__class__.__name__}"}


def read_nfo(path: str) -> dict[str, Any]:
    """Parse Kodi tvshow.nfo / episodedetails / movie .nfo. Non-XML NFOs (scene info) yield {}."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(256 * 1024)
        root = ET.fromstring(data)
    except Exception:
        return {}
    tag = root.tag.lower()
    def t(name: str):
        el = root.find(name)
        return el.text.strip() if el is not None and el.text and el.text.strip() else None
    out: dict[str, Any] = {"_source": "nfo", "nfo_type": tag}
    if tag == "tvshow":
        out["series"] = t("title")
        if (y := t("year")) and y.isdigit():
            out["series_year"] = int(y)
    elif tag == "episodedetails":
        out["episode_title"] = t("title")
        out["series"] = t("showtitle")
        if (s := t("season")) and s.lstrip("-").isdigit():
            out["season"] = int(s)
        if (e := t("episode")) and e.isdigit():
            out["episodes"] = [int(e)]
        if a := t("aired"):
            out["air_date"] = a
    elif tag == "movie":
        out["title"] = t("title")
        if (y := t("year")) and y.isdigit():
            out["year"] = int(y)
    return {k: v for k, v in out.items() if v is not None}
