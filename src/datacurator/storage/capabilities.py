"""Per-destination capability detection.

Behaviour is detected per destination, never assumed from the filesystem name
alone. `inspect()` is passive (reads mount tables, pathconf, statvfs); it is
used for previews so previews never write anything. `probe()` is active: it
creates and removes a hidden probe file inside the destination to measure case
sensitivity, Unicode normalization, link and xattr support. Apply-time
preflight always runs an active probe and refuses to execute a plan whose
naming decisions would differ.
"""
from __future__ import annotations

import errno
import os
import secrets
import shutil
import sys
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

NETWORK_FS = {"cifs", "smb", "smb2", "smb3", "smbfs", "nfs", "nfs4", "afpfs", "fuse.sshfs",
              "sshfs", "9p", "davfs", "fuse.davfs", "webdav", "ncpfs", "fuse.rclone", "virtiofs",
              "fuse.smbnetfs", "fuse.gvfsd-fuse", "fuse.s3fs", "smb2fs"}
WINDOWS_STYLE_FS = {"ntfs", "ntfs3", "fuseblk", "vfat", "msdos", "fat", "fat32", "exfat",
                    "fuse.exfat", "fuse.fusefat", "refs", "cifs", "smb", "smb2", "smb3", "smbfs", "fuse.smbnetfs"}
CASE_INSENSITIVE_FS = {"vfat", "msdos", "fat", "fat32", "exfat", "fuse.exfat", "fuse.fusefat", "hfs", "apfs",
                       "cifs", "smb", "smb2", "smb3", "smbfs", "fuse.smbnetfs"}
READ_ONLY_FS = {"iso9660", "udf", "squashfs", "cramfs", "erofs", "cd9660"}
FAT_FS = {"vfat", "msdos", "fat", "fat32", "fuse.fusefat"}

WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10)), "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
WIN_FORBIDDEN = '<>:"/\\|?*'


@dataclass(slots=True)
class VolumeIdentity:
    fs_type: str = ""
    mountpoint: str = ""
    device: str = ""          # mount source: /dev/sda1, //nas/share, server:/export
    st_dev: int = 0
    root_ino: int = 0         # inode of the selected root folder itself
    fsid: int = 0             # statvfs f_fsid (stable for local filesystems)
    win_serial: int = 0
    is_network: bool = False
    path: str = ""            # the existing folder these values were read from

    def matches(self, other: "VolumeIdentity") -> tuple[bool, str]:
        """True if `other` plausibly is the same storage. Returns (ok, explanation)."""
        if self.fs_type and other.fs_type and self.fs_type != other.fs_type:
            return False, f"filesystem type changed ({self.fs_type} → {other.fs_type})"
        if self.root_ino and other.root_ino and self.root_ino != other.root_ino:
            return False, "the folder at this path is not the same folder that was scanned"
        if self.win_serial and other.win_serial and self.win_serial != other.win_serial:
            return False, "volume serial number changed"
        if self.is_network:
            if self.device and other.device and self.device != other.device:
                return False, f"share source changed ({self.device} → {other.device})"
        else:
            if self.fsid and other.fsid and self.fsid != other.fsid:
                return False, "a different disk is mounted at this path"
        return True, "same storage"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "VolumeIdentity":
        return cls(**(d or {}))


@dataclass(slots=True)
class Capabilities:
    path: str
    fs_type: str = "unknown"
    probed: str = "inferred"          # inferred | active
    case_sensitive: bool = True
    normalization_insensitive: bool = False  # NFC and NFD names refer to the same file
    windows_naming: bool = False     # forbidden chars / reserved names / trailing dot-space matter
    max_name_bytes: int = 255         # UTF-8 bytes (POSIX)
    max_name_units: int = 255         # UTF-16 code units (Windows/NTFS/exFAT/FAT LFN)
    max_path: int = 4096
    max_file_size: int | None = None
    free_bytes: int | None = None
    read_only: bool = False
    is_network: bool = False
    supports_symlinks: bool = True
    supports_hardlinks: bool = True
    supports_xattr: bool | None = None
    supports_acl: bool | None = None
    supports_ads: bool = False
    timestamp_resolution_ns: int = 1
    ascii_only: bool = False          # driver rejects non-ASCII names (measured by probe)
    identity: VolumeIdentity = field(default_factory=VolumeIdentity)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Capabilities":
        d = dict(d)
        d["identity"] = VolumeIdentity.from_dict(d.get("identity"))
        return cls(**d)

    def naming_signature(self) -> tuple:
        """Fields that influence generated names; a mismatch at apply time invalidates a plan."""
        return (self.case_sensitive, self.normalization_insensitive, self.windows_naming,
                self.max_name_bytes, self.max_name_units, self.max_path, self.ascii_only)


def _mount_for(path: str):
    """Return (mountpoint, fstype, device) for path using psutil, longest-prefix match."""
    if psutil is None:
        return "", "unknown", ""
    rp = os.path.realpath(path)
    best = None
    try:
        parts = psutil.disk_partitions(all=True)
    except Exception:
        parts = []
    for p in parts:
        mp = p.mountpoint
        if IS_WIN:
            if rp.lower().startswith(mp.lower()) and (best is None or len(mp) > len(best.mountpoint)):
                best = p
        else:
            if (rp == mp or rp.startswith(mp.rstrip("/") + "/") or mp == "/") and \
                    (best is None or len(mp) > len(best.mountpoint)):
                best = p
    if best is None:
        return "", "unknown", ""
    return best.mountpoint, (best.fstype or "unknown").lower(), best.device


def _win_volume_info(path: str) -> dict[str, Any]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    if not k32.GetVolumePathNameW(ctypes.c_wchar_p(os.path.abspath(path)), buf, 1024):
        return {}
    root = buf.value
    name_buf = ctypes.create_unicode_buffer(261)
    fs_buf = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_comp = wintypes.DWORD()
    flags = wintypes.DWORD()
    ok = k32.GetVolumeInformationW(ctypes.c_wchar_p(root), name_buf, 261, ctypes.byref(serial),
                                   ctypes.byref(max_comp), ctypes.byref(flags), fs_buf, 261)
    drive_type = k32.GetDriveTypeW(ctypes.c_wchar_p(root))
    if not ok:
        return {"root": root, "drive_type": drive_type}
    f = flags.value
    return {
        "root": root, "fs": fs_buf.value.lower(), "serial": serial.value, "max_comp": max_comp.value,
        "case_sensitive": bool(f & 0x1), "case_preserved": bool(f & 0x2), "acl": bool(f & 0x8),
        "ads": bool(f & 0x40000), "read_only": bool(f & 0x80000), "hardlinks": bool(f & 0x400000),
        "drive_type": drive_type,
    }


def _win_long_paths_enabled() -> bool:  # pragma: no cover - Windows only
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except Exception:
        return False


def volume_identity(path: str) -> VolumeIdentity:
    st = os.stat(path)
    mp, fstype, dev = _mount_for(path)
    vid = VolumeIdentity(fs_type=fstype, mountpoint=mp, device=dev, st_dev=st.st_dev,
                         root_ino=st.st_ino, is_network=fstype in NETWORK_FS, path=os.path.abspath(path))
    if hasattr(os, "statvfs"):
        try:
            vid.fsid = int(os.statvfs(path).f_fsid)
        except Exception:
            pass
    if IS_WIN:  # pragma: no cover
        info = _win_volume_info(path)
        if info.get("fs"):
            vid.fs_type = info["fs"]
        vid.win_serial = info.get("serial", 0)
        if info.get("drive_type") == 4 or path.startswith("\\\\"):
            vid.is_network = True
    return vid


def nearest_existing(path: str) -> str:
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return p


def inspect(path: str) -> Capabilities:
    """Passive inspection; never writes. `path` may not exist yet (nearest ancestor is used)."""
    base = nearest_existing(path)
    caps = Capabilities(path=os.path.abspath(path))
    try:
        caps.identity = volume_identity(base)
    except OSError as e:
        caps.notes.append(f"Could not read volume identity: {e.strerror}")
    fs = caps.identity.fs_type or "unknown"
    caps.fs_type = fs
    caps.is_network = caps.identity.is_network or path.startswith("\\\\")
    try:
        du = shutil.disk_usage(base)
        if du.total > 0:
            caps.free_bytes = du.free
        else:
            caps.notes.append("This filesystem does not report free space; space is checked while copying.")
    except OSError:
        pass

    if IS_WIN:  # pragma: no cover
        info = _win_volume_info(base)
        caps.windows_naming = True
        caps.case_sensitive = bool(info.get("case_sensitive", False))
        caps.supports_acl = info.get("acl")
        caps.supports_ads = bool(info.get("ads"))
        caps.supports_hardlinks = bool(info.get("hardlinks", False))
        caps.read_only = bool(info.get("read_only", False))
        caps.max_name_units = int(info.get("max_comp", 255) or 255)
        caps.max_path = 32767 if _win_long_paths_enabled() else 259
        caps.supports_symlinks = True
        caps.notes.append("Symlink creation on Windows needs Developer Mode or admin rights.")
    else:
        caps.windows_naming = fs in WINDOWS_STYLE_FS
        caps.case_sensitive = fs not in CASE_INSENSITIVE_FS
        if IS_MAC and fs in {"apfs", "hfs"}:
            caps.normalization_insensitive = True
            caps.notes.append("macOS volumes are usually case-insensitive; an active probe confirms.")
        if fs == "fuseblk":
            caps.notes.append("fuseblk is a FUSE block filesystem (commonly NTFS via ntfs-3g or exFAT); "
                              "Windows naming rules are applied and an active probe measures the rest.")
        if hasattr(os, "pathconf"):
            try:
                caps.max_name_bytes = min(255, os.pathconf(base, "PC_NAME_MAX"))
            except (OSError, ValueError):
                pass
            try:
                caps.max_path = os.pathconf(base, "PC_PATH_MAX")
            except (OSError, ValueError):
                pass
        if hasattr(os, "statvfs"):
            try:
                caps.read_only = bool(os.statvfs(base).f_flag & getattr(os, "ST_RDONLY", 1))
            except OSError:
                pass
        if fs in FAT_FS:
            caps.max_file_size = 4 * 1024**3 - 1
            caps.supports_symlinks = caps.supports_hardlinks = False
            caps.supports_xattr = False
            caps.timestamp_resolution_ns = 2_000_000_000
            caps.notes.append("FAT32: 4 GiB file size limit, 2-second timestamps, no permissions or links.")
        if fs in {"exfat", "fuse.exfat"}:
            caps.supports_symlinks = caps.supports_hardlinks = False
            caps.supports_xattr = False
            caps.timestamp_resolution_ns = 10_000_000
            caps.notes.append("exFAT: no permissions, links or extended attributes.")
        if fs in {"ntfs", "ntfs3", "fuseblk"}:
            caps.max_name_units = 255
        if fs in NETWORK_FS:
            caps.notes.append("Network share: behaviour depends on the server; run 'Check destination' to measure it.")
    if fs in READ_ONLY_FS:
        caps.read_only = True
        caps.notes.append(f"{fs} is read-only; it can be scanned but not modified.")
    if caps.read_only and not any("read-only" in n for n in caps.notes):
        caps.notes.append("This location is mounted read-only.")
    return caps


def probe(path: str) -> Capabilities:
    """Active probe: creates and removes a hidden probe file in an existing directory."""
    caps = inspect(path)
    base = nearest_existing(path)
    if not os.path.isdir(base):
        caps.notes.append("Destination parent is not a directory; active probe skipped.")
        return caps
    if caps.read_only:
        return caps
    token = secrets.token_hex(4)
    lower = os.path.join(base, f".curator-probe-{token}-a")
    created: list[str] = []
    try:
        try:
            fd = os.open(lower, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created.append(lower)
        except OSError as e:
            if e.errno in (errno.EROFS,):
                caps.read_only = True
            elif e.errno in (errno.EACCES, errno.EPERM):
                caps.notes.append("No permission to create files here.")
            else:
                caps.notes.append(f"Probe failed: {e.strerror}")
            return caps
        caps.probed = "active"
        upper = os.path.join(base, f".CURATOR-PROBE-{token}-A")
        caps.case_sensitive = not os.path.exists(upper)
        # Some drivers (e.g. userspace FAT) refuse non-ASCII names entirely
        uni = os.path.join(base, f".curator-probe-{token}-ü")
        try:
            fd = os.open(uni, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created.append(uni)
        except OSError as e:
            if e.errno in (errno.EPERM, errno.EINVAL, errno.EILSEQ, errno.EIO):
                caps.ascii_only = True
                caps.notes.append("This filesystem driver rejects non-ASCII filenames; names will be transliterated.")
        # Unicode normalization: create NFD name, look it up as NFC
        nfd = os.path.join(base, unicodedata.normalize("NFD", f".curator-probe-{token}-é"))
        nfc = os.path.join(base, unicodedata.normalize("NFC", f".curator-probe-{token}-é"))
        try:
            fd = os.open(nfd, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created.append(nfd)
            caps.normalization_insensitive = os.path.exists(nfc) and nfc != nfd
        except OSError:
            pass
        # Trailing dot / reserved names behave as on Windows?
        if not IS_WIN:
            for bad in (f".curator-probe-{token}-x.", f".curator-probe-{token}-q?"):
                p = os.path.join(base, bad)
                try:
                    fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                    created.append(p)
                    if not os.path.exists(p) or bad not in os.listdir(base):
                        caps.windows_naming = True
                except OSError:
                    caps.windows_naming = True
        # symlinks / hardlinks
        s = os.path.join(base, f".curator-probe-{token}-s")
        try:
            os.symlink(lower, s)
            created.append(s)
            caps.supports_symlinks = True
        except (OSError, NotImplementedError):
            caps.supports_symlinks = False
        h = os.path.join(base, f".curator-probe-{token}-h")
        try:
            os.link(lower, h)
            created.append(h)
            caps.supports_hardlinks = True
        except (OSError, NotImplementedError):
            caps.supports_hardlinks = False
        if hasattr(os, "setxattr") and not xattr_safe(base):
            caps.supports_xattr = False
        elif hasattr(os, "setxattr"):
            try:
                os.setxattr(lower, "user.curator.probe", b"1")
                caps.supports_xattr = True
            except OSError:
                caps.supports_xattr = False
        # Name length: try a 255-byte ASCII name
        long_name = os.path.join(base, (f".curator-probe-{token}-" + "n" * 255)[:255])
        try:
            fd = os.open(long_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created.append(long_name)
        except OSError as e:
            if e.errno == errno.ENAMETOOLONG:
                caps.max_name_bytes = min(caps.max_name_bytes, 143)
                caps.notes.append("Filenames are limited below 255 bytes here (e.g. encrypted home).")
    finally:
        for p in reversed(created):
            try:
                os.unlink(p)
            except OSError:
                pass
    return caps


def check_reachable(path: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Check a path without hanging forever on a stale network mount."""
    import threading
    result: dict[str, Any] = {}

    def worker():
        try:
            result["ok"] = os.path.isdir(path)
            if result["ok"]:
                os.listdir(path)
        except OSError as e:
            result["ok"] = False
            result["err"] = e.strerror or str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, f"No response after {timeout:.0f}s; the share may be disconnected or the mount is stale."
    if result.get("ok"):
        return True, "Reachable"
    return False, result.get("err", "Folder not found")


_XATTR_CACHE: dict[str, bool] = {}


def xattr_safe(path: str) -> bool:
    """Whether extended-attribute calls are supported and safe on the filesystem holding `path`.

    Some FUSE network clients fail hard (and wedge the mount) on xattr calls, so
    network, FAT and exFAT filesystems are never asked.
    """
    mp, fstype, _ = _mount_for(path)
    if mp in _XATTR_CACHE:
        return _XATTR_CACHE[mp]
    ok = not (fstype in NETWORK_FS or fstype in FAT_FS or fstype in {"exfat", "fuse.exfat", "fuseblk", "vfat"}
              or fstype.startswith("fuse.smb") or fstype in READ_ONLY_FS)
    _XATTR_CACHE[mp] = ok
    return ok
