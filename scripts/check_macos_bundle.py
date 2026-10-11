"""Verify the built GUI can own the macOS menu bar, independently of Qt's offscreen smoke test."""
import plistlib
import sys
from pathlib import Path


def check_bundle(bundle):
    bundle = Path(bundle)
    with (bundle / "Contents" / "Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    for key in ("LSBackgroundOnly", "LSUIElement"):
        if info.get(key, False) is not False:
            raise ValueError(f"{key} must be false: Data Curator must launch as a foreground app")
    if info.get("CFBundleExecutable") != "DataCurator":
        raise ValueError("The bundle must launch the DataCurator GUI, not the curator CLI")
    if not (bundle / "Contents" / "MacOS" / "DataCurator").is_file():
        raise ValueError("The GUI executable is missing")
    if info.get("CFBundleName") != "Data Curator":
        raise ValueError("The native application menu must be named Data Curator")
    print("macOS GUI bundle: foreground app, native menu-bar ownership enabled")


if __name__ == "__main__":
    check_bundle(sys.argv[1])
