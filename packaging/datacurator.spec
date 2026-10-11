# PyInstaller spec: builds "DataCurator" (desktop app) and "curator" (CLI) into one folder.
# Build on each target OS:   pyinstaller packaging/datacurator.spec --noconfirm
import os, sys
block_cipher = None
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
hidden = ["keyring.backends", "mutagen", "PIL.Image"]

gui = Analysis([os.path.join(ROOT, "packaging", "run_gui.py")], pathex=[SRC], hiddenimports=hidden,
               excludes=["tkinter", "pytest"], cipher=block_cipher)
cli = Analysis([os.path.join(ROOT, "packaging", "run_cli.py")], pathex=[SRC], hiddenimports=hidden,
               excludes=["tkinter", "pytest", "PySide6"], cipher=block_cipher)
MERGE((gui, "run_gui", "DataCurator"), (cli, "run_cli", "curator"))

gui_pyz = PYZ(gui.pure, gui.zipped_data, cipher=block_cipher)
cli_pyz = PYZ(cli.pure, cli.zipped_data, cipher=block_cipher)
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name="DataCurator", console=False,
              icon=os.path.join(ROOT, "packaging", "icon.ico") if sys.platform == "win32" else None)
cli_exe = EXE(cli_pyz, cli.scripts, [], exclude_binaries=True, name="curator", console=True)
coll = COLLECT(gui_exe, gui.binaries, gui.zipfiles, gui.datas, cli_exe, cli.binaries, cli.zipfiles, cli.datas,
               name="DataCurator")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Data Curator.app", bundle_identifier="org.datacurator.app",
                 info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
