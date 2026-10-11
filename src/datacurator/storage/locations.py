"""Saved locations (local folders, removable drives, SMB/NFS shares).

Connections use the operating system's own facilities; credentials live in the
OS credential store via `keyring` (Windows Credential Manager, macOS Keychain,
Secret Service on Linux). Nothing secret is written to the catalog.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .capabilities import VolumeIdentity, check_reachable, volume_identity

KEYRING_SERVICE = "DataCurator"


@dataclass
class LocationStatus:
    name: str
    path: str
    reachable: bool
    message: str
    same_storage: bool | None = None
    guidance: str = ""


def store_password(name: str, username: str, password: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, f"{name}:{username}", password)


def get_password(name: str, username: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, f"{name}:{username}")
    except Exception:
        return None


def status(loc: dict[str, Any], timeout: float = 5.0) -> LocationStatus:
    ok, msg = check_reachable(loc["path"], timeout)
    st = LocationStatus(loc["name"], loc["path"], ok, msg)
    if ok and loc.get("identity"):
        same, why = VolumeIdentity.from_dict(loc["identity"]).matches(volume_identity(loc["path"]))
        st.same_storage = same
        if not same:
            st.message = f"Reachable, but {why}."
            st.guidance = "Something else is mounted at this path. Check which share or drive is connected."
    if not ok:
        st.guidance = connect_guidance(loc)
    return st


def connect_command(loc: dict[str, Any], platform: str | None = None) -> list[str] | None:
    """OS command that (re)connects a saved share; None for local locations."""
    platform = platform or sys.platform
    proto, server, share, user = loc.get("protocol"), loc.get("server"), loc.get("share"), loc.get("username")
    if not proto or not server or not share:
        return None
    if platform.startswith("win"):
        if proto != "smb":
            return None
        cmd = ["net", "use", f"\\\\{server}\\{share}"]
        if user:
            cmd.append(f"/user:{user}")
        cmd.append("/persistent:no")
        return cmd
    if platform == "darwin":
        url = f"{proto}://{(user + '@') if user else ''}{server}/{share}"
        return ["open", url]  # Finder mounts and uses the Keychain
    if proto == "smb":
        return ["gio", "mount", f"smb://{(user + '@') if user else ''}{server}/{share}"]
    if proto == "nfs":
        return ["mount", f"{server}:/{share.lstrip('/')}", loc["path"]]  # usually needs an fstab entry / root
    return None


def connect_guidance(loc: dict[str, Any]) -> str:
    cmd = connect_command(loc)
    path = loc["path"]
    if cmd:
        return "Reconnect with: " + " ".join(shlex.quote(c) for c in cmd)
    if path.startswith("\\\\"):
        return "Open the share in File Explorer to reconnect it, then refresh."
    if sys.platform == "darwin" and path.startswith("/Volumes/"):
        return "Reconnect the drive or use Finder ▸ Go ▸ Connect to Server."
    if sys.platform.startswith("linux") and (path.startswith("/media/") or path.startswith("/run/media/")):
        return "Plug the drive back in; your desktop mounts it at the same place."
    return "Make sure the drive or share is connected and mounted at this path."


def connect(loc: dict[str, Any], timeout: float = 60) -> tuple[bool, str]:
    cmd = connect_command(loc)
    if not cmd:
        return False, connect_guidance(loc)
    env = dict(os.environ)
    inp = None
    if sys.platform.startswith("win") and loc.get("username"):
        pw = get_password(loc["name"], loc["username"])
        if pw:
            cmd = cmd[:3] + [pw] + cmd[3:]  # net use \\s\share <password> /user:...
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=inp, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"Could not run the connect command: {e}"
    if r.returncode == 0:
        ok, msg = check_reachable(loc["path"])
        return ok, ("Connected." if ok else f"Command succeeded but {loc['path']} is not reachable yet: {msg}")
    return False, (r.stderr or r.stdout or "connect failed").strip()
