"""Storage adapter: every mutating filesystem call the executor makes goes through here.

Replace or subclass `LocalFS` to support other storage or to inject faults in tests.
"""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import sys

from ..engine.util import fsync_dir

_RENAME_NOREPLACE = 1
_AT_FDCWD = -100
_libc = None


def _get_libc():
    global _libc
    if _libc is None:
        try:
            _libc = ctypes.CDLL(None, use_errno=True)
        except OSError:  # pragma: no cover
            _libc = False
    return _libc


class LocalFS:
    """Operating-system filesystem access with no-overwrite renames."""

    name = "local"

    # -- queries
    def stat(self, p: str) -> os.stat_result:
        return os.stat(p)

    def lstat(self, p: str) -> os.stat_result:
        return os.lstat(p)

    def exists(self, p: str) -> bool:
        return os.path.lexists(p)

    def isdir(self, p: str) -> bool:
        return os.path.isdir(p)

    def listdir(self, p: str) -> list[str]:
        return os.listdir(p)

    def samefile(self, a: str, b: str) -> bool:
        try:
            return os.path.samefile(a, b)
        except OSError:
            return False

    def free_bytes(self, p: str) -> int:
        return shutil.disk_usage(p).free

    # -- mutations
    def mkdir(self, p: str) -> None:
        os.mkdir(p)

    def rmdir(self, p: str) -> None:
        os.rmdir(p)  # only ever removes an empty directory

    def unlink(self, p: str) -> None:
        os.unlink(p)

    def fsync_dir(self, p: str) -> None:
        fsync_dir(p)

    def copystat(self, src: str, dst: str) -> list[str]:
        """Copy timestamps/mode (and xattrs where Python supports it). Returns notes on anything lost."""
        notes = []
        try:
            shutil.copystat(src, dst, follow_symlinks=False)
        except OSError as e:
            notes.append(f"some attributes could not be copied ({e.strerror})")
        return notes

    def open_read(self, p: str):
        return open(p, "rb")

    def open_write(self, p: str, append: bool = False):
        if append:
            return open(p, "r+b")
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o644)
        return os.fdopen(fd, "wb")

    def replace_own(self, src: str, dst: str) -> None:
        """Atomic replace; used only for files this application created or backed up."""
        os.replace(src, dst)

    def rename_noreplace(self, src: str, dst: str) -> str:
        """Rename without ever replacing an existing destination. Returns the method used.

        Raises FileExistsError if dst exists.
        """
        if sys.platform.startswith("linux"):
            libc = _get_libc()
            fn = getattr(libc, "renameat2", None) if libc else None
            if fn is not None:
                r = fn(_AT_FDCWD, os.fsencode(src), _AT_FDCWD, os.fsencode(dst), _RENAME_NOREPLACE)
                if r == 0:
                    return "renameat2"
                e = ctypes.get_errno()
                if e == errno.EEXIST:
                    raise FileExistsError(errno.EEXIST, "Destination exists", dst)
                if e not in (errno.EINVAL, errno.ENOSYS, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise OSError(e, os.strerror(e), src)
        elif sys.platform == "darwin":  # pragma: no cover - macOS only
            libc = _get_libc()
            fn = getattr(libc, "renamex_np", None) if libc else None
            if fn is not None:
                r = fn(os.fsencode(src), os.fsencode(dst), 0x4)  # RENAME_EXCL
                if r == 0:
                    return "renamex_np"
                e = ctypes.get_errno()
                if e == errno.EEXIST:
                    raise FileExistsError(errno.EEXIST, "Destination exists", dst)
                if e not in (errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
                    raise OSError(e, os.strerror(e), src)
        elif os.name == "nt":  # pragma: no cover - Windows only
            os.rename(src, dst)  # MoveFileExW without REPLACE_EXISTING: fails if dst exists
            return "MoveFileEx"
        # Fallback 1: hard link + unlink is an atomic no-replace on most POSIX filesystems
        try:
            os.link(src, dst, follow_symlinks=False)
            os.unlink(src)
            return "link+unlink"
        except FileExistsError:
            raise
        except OSError as e:
            if e.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK, errno.EXDEV,
                               errno.ENOSYS, errno.EACCES):
                raise
        # Fallback 2: check-then-rename (small race window; documented limitation)
        if os.path.lexists(dst):
            raise FileExistsError(errno.EEXIST, "Destination exists", dst)
        os.rename(src, dst)
        return "checked-rename"
