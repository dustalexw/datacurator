"""Hashing, path safety and small filesystem helpers."""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict
from typing import Callable

CHUNK = 1024 * 1024


def hash_file(path: str, cancel: threading.Event | None = None,
              progress: Callable[[int], None] | None = None, limit: int | None = None) -> str:
    """BLAKE2b-256 of a file (or its first `limit` bytes)."""
    h = hashlib.blake2b(digest_size=32)
    done = 0
    with open(path, "rb") as fh:
        while True:
            want = CHUNK if limit is None else min(CHUNK, limit - done)
            if want <= 0:
                break
            b = fh.read(want)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(len(b))
            if cancel is not None and cancel.is_set():
                raise InterruptedError("cancelled")
    return h.hexdigest()


def stat_sig(st: os.stat_result) -> str:
    return f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}"


def is_within(path: str, root: str) -> bool:
    """True if `path` is `root` or inside it, after normalization (no symlink resolution)."""
    p = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    try:
        return os.path.commonpath([p, r]) == r
    except ValueError:  # different drives on Windows
        return False


def is_within_real(path: str, root: str) -> bool:
    """Like is_within but resolves symlinks on the existing part of `path`."""
    existing = path
    tail = []
    while not os.path.exists(existing):
        parent, name = os.path.split(existing)
        if parent == existing:
            break
        tail.append(name)
        existing = parent
    real = os.path.join(os.path.realpath(existing), *reversed(tail))
    return is_within(real, os.path.realpath(root))


def fsync_dir(path: str) -> None:
    if os.name == "nt":  # directories cannot be fsynced on Windows; NTFS journals metadata
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class DirListingCache:
    """Bounded cache of directory listings used for collision checks."""

    def __init__(self, max_dirs: int = 4096):
        self._d: OrderedDict[str, dict[str, str] | None] = OrderedDict()
        self.max = max_dirs

    def listing(self, d: str, keyfn: Callable[[str], str]) -> dict[str, str] | None:
        """key -> actual name; None if the directory does not exist."""
        if d in self._d:
            self._d.move_to_end(d)
            return self._d[d]
        try:
            names = os.listdir(d)
            val = {keyfn(n): n for n in sorted(names)}
        except FileNotFoundError:
            val = None
        except NotADirectoryError:
            val = None
        except OSError:
            val = None
        self._d[d] = val
        if len(self._d) > self.max:
            self._d.popitem(last=False)
        return val


def same_entry(a: str, b: str) -> bool:
    """True if paths `a` and `b` name the same directory entry.

    Uses inode identity, and falls back to a listing check for filesystems
    (FUSE FAT/exFAT, SMB clients) whose inode numbers are not stable: on a
    case-insensitive volume "x/Song.flac" and "x/song.flac" are the same entry
    when only one of the two spellings is listed.
    """
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        return False
    da, na = os.path.split(a)
    db, nb = os.path.split(b)
    if os.path.normcase(da) != os.path.normcase(db) or na == nb:
        return False
    import unicodedata
    if unicodedata.normalize("NFC", na).casefold() != unicodedata.normalize("NFC", nb).casefold():
        return False
    try:
        names = set(os.listdir(da))
    except OSError:
        return False
    return (na in names) != (nb in names)
