"""Isolated fixture builders. Every test works inside pytest's tmp_path only."""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TCMP, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC
from mutagen.mp4 import MP4

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "_audio_templates")

# ffmpeg encoders for 0.2 s of silence per container
ENCODERS = {
    ".flac": ["-c:a", "flac"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "32k"],
    ".m4a": ["-c:a", "aac", "-b:a", "32k"],
    ".alac.m4a": ["-c:a", "alac"],
    ".ogg": ["-c:a", "libvorbis"],
    ".opus": ["-c:a", "libopus", "-b:a", "16k"],
    ".wav": ["-c:a", "pcm_s16le"],
    ".aiff": ["-c:a", "pcm_s16be"],
    ".wma": ["-c:a", "wmav2", "-b:a", "32k"],
}


FALLBACK = {
    ".ogg": [["-c:a", "vorbis", "-strict", "-2"]],
    ".opus": [["-c:a", "opus", "-strict", "-2", "-b:a", "32k"]],
    ".mp3": [["-c:a", "mp3"]],
}


@lru_cache(maxsize=None)
def template(ext: str) -> str:
    """Create (once) a tiny real audio file of the given type with ffmpeg."""
    os.makedirs(TEMPLATE_DIR, exist_ok=True)
    out = os.path.join(TEMPLATE_DIR, "t" + ext)
    if not os.path.exists(out):
        real_ext = ".m4a" if ext == ".alac.m4a" else ext
        tmp = out + ".tmp" + real_ext
        # Some ffmpeg builds (e.g. Homebrew) lack libvorbis/libopus: fall back to ffmpeg's native encoders
        options = [ENCODERS[ext]] + FALLBACK.get(ext, [])
        for i, enc in enumerate(options):
            src = "anullsrc=r=48000:cl=stereo" if i else "anullsrc=r=44100:cl=mono"
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", src, "-t", "0.2", *enc, tmp]
            try:
                subprocess.run(cmd, check=True)
                break
            except subprocess.CalledProcessError:
                if i == len(options) - 1:
                    raise
        os.replace(tmp, out)
    return out


def make_audio(path: str, ext: str = ".flac", **tags) -> str:
    """Copy a template and write tags. Keys: title, artist, album, albumartist, tracknumber,
    discnumber, date, compilation, genre. A `salt` key makes content unique."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    shutil.copyfile(template(ext), path)
    salt = tags.pop("salt", None)
    if tags:
        if ext in (".mp3", ".wav", ".aiff"):
            f = mutagen.File(path)
            if f.tags is None:
                f.add_tags()
            t = f.tags
            mp = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2, "tracknumber": TRCK,
                  "discnumber": TPOS, "date": TDRC}
            for k, v in tags.items():
                if k == "compilation":
                    t.add(TCMP(encoding=3, text=["1" if v else "0"]))
                elif k in mp:
                    t.add(mp[k](encoding=3, text=[str(v)]))
            f.save()
        elif ext in (".m4a", ".alac.m4a"):
            f = MP4(path)
            mp = {"title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb", "albumartist": "aART",
                  "date": "\xa9day", "genre": "\xa9gen"}
            for k, v in tags.items():
                if k == "tracknumber":
                    f["trkn"] = [(int(str(v).split("/")[0]), 0)]
                elif k == "discnumber":
                    n = str(v).split("/")
                    f["disk"] = [(int(n[0]), int(n[1]) if len(n) > 1 else 0)]
                elif k == "compilation":
                    f["cpil"] = bool(v)
                elif k in mp:
                    f[mp[k]] = [str(v)]
            f.save()
        elif ext == ".wma":
            from mutagen.asf import ASF
            f = ASF(path)
            mp = {"title": "Title", "artist": "Author", "album": "WM/AlbumTitle", "albumartist": "WM/AlbumArtist",
                  "tracknumber": "WM/TrackNumber", "discnumber": "WM/PartOfSet", "date": "WM/Year"}
            for k, v in tags.items():
                if k in mp:
                    f[mp[k]] = [str(v)]
            f.save()
        else:
            f = mutagen.File(path)
            if f.tags is None:
                f.add_tags()
            for k, v in tags.items():
                if k == "compilation":
                    f.tags["COMPILATION"] = ["1" if v else "0"]
                else:
                    f.tags[k.upper()] = [str(v)]
            f.save()
    if salt is not None:
        # append bytes to a trailing padding region readers ignore: use a tag field instead
        f = mutagen.File(path)
        if hasattr(f, "tags") and f.tags is not None and isinstance(f, FLAC):
            f.tags["SALT"] = [str(salt)]
            f.save()
        else:
            with open(path, "ab") as fh:
                fh.write(f"SALT{salt}".encode())
    return path


def make_file(path: str, content: bytes | str = b"x", mtime: float | None = None) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if isinstance(content, str):
        content = content.encode()
    with open(path, "wb") as fh:
        fh.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def make_photo(path: str, date: str | None = "2021:07:04 10:00:00", size=(8, 8)) -> str:
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", size, (200, 100, 50))
    exif = Image.Exif()
    if date:
        exif[306] = date
        exif[271] = "TestCam"
    im.save(path, exif=exif)
    return path


def snapshot(root: str) -> dict[str, tuple[int, int, bytes]]:
    """Every file under root -> (size, mtime_ns, first 64 bytes) and every dir; used to prove no mutation."""
    out = {}
    for dp, dns, fns in os.walk(root):
        for d in dns:
            st = os.lstat(os.path.join(dp, d))
            out[os.path.relpath(os.path.join(dp, d), root) + "/"] = (0, st.st_mtime_ns, b"")
        for f in fns:
            p = os.path.join(dp, f)
            st = os.lstat(p)
            with open(p, "rb") as fh:
                head = fh.read()
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, head)
    return out
