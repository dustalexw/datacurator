# Installation and packaging

## Run from source (all platforms)

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[gui,dev]"
datacurator          # desktop app
curator --help       # command line
python -m pytest     # test suite
```

Requirements: Python 3.11 or newer. Dependencies (installed automatically): `mutagen`, `Pillow`,
`psutil`, `platformdirs`, `keyring`, and `PySide6` for the desktop app. `ffmpeg` is only needed to
generate audio test fixtures.

## Standalone builds (PyInstaller)

Build on the platform you are targeting (PyInstaller does not cross-compile):

```bash
python -m pip install -e ".[gui,dev]"
pyinstaller packaging/datacurator.spec --noconfirm
```

The output folder `dist/DataCurator/` contains both programs sharing one runtime:
`DataCurator` (desktop app, no console) and `curator` (CLI).

### Windows

* Output: `dist\DataCurator\DataCurator.exe` and `curator.exe`.
* Installer (optional): point Inno Setup or WiX at `dist\DataCurator\`; add `curator.exe`'s folder
  to `PATH` if you want the CLI everywhere.
* Long paths: enable *Win32 long paths* (Group Policy or `LongPathsEnabled=1`) to allow paths over 260
  characters; without it the preview shortens or flags such paths.
* Sign the executables with `signtool` to avoid SmartScreen warnings.

### macOS

* Output: `dist/Data Curator.app` (plus the `curator` CLI inside `dist/DataCurator/`).
* Sign and notarize for distribution:
  `codesign --deep --options runtime --sign "Developer ID Application: …" "dist/Data Curator.app"`,
  then `xcrun notarytool submit … --wait` and `xcrun stapler staple`.
* Grant *Full Disk Access* (System Settings ▸ Privacy & Security) if you organize folders macOS
  protects (Desktop, Documents, removable volumes, network volumes).

### Linux

* Output: `dist/DataCurator/` — run `./DataCurator` or `./curator`. Verified on Ubuntu 24.04 userland.
* Package as a tarball, or wrap the folder with `appimagetool` (AppImage) or a `.desktop` file:

```ini
[Desktop Entry]
Type=Application
Name=Data Curator
Exec=/opt/DataCurator/DataCurator
Categories=Utility;FileTools;
```

* Qt needs a UTF-8 locale; the app switches to `C.UTF-8` automatically when the system locale is `C`.
* Saved share passwords use the Secret Service (GNOME Keyring / KWallet). Without one, `keyring`
  reports that no backend is available and the password is not stored.

## Data locations

| | Catalog, plans, journal |
|---|---|
| Linux | `~/.local/share/DataCurator` |
| macOS | `~/Library/Application Support/DataCurator` |
| Windows | `%LOCALAPPDATA%\DataCurator\DataCurator` |
| Any | `DATACURATOR_HOME` overrides, `--data-dir` for the CLI |

Keep this folder on a local disk: it holds the recovery journal and the backups used to undo
reference rewrites and tag writes.

## Scheduling unattended runs

Enable it in the profile, then use the OS scheduler:

```json
"unattended": {"enabled": true, "conflict_policy": "skip", "resume_interrupted": true}
```

```bash
# Linux/macOS crontab: every night at 02:30
30 2 * * * /opt/DataCurator/curator -q run -p /home/me/profiles/nas.json >> /home/me/curator.log 2>&1
```

Windows Task Scheduler: action `C:\Program Files\DataCurator\curator.exe`, arguments
`-q run -p C:\Users\me\profiles\nas.json`.
