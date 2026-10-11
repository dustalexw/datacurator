# PyInstaller spec: builds "DataCurator" (desktop app) and "curator" (CLI) into one folder.
# Build on each target OS:   pyinstaller packaging/datacurator.spec --noconfirm
import os, sys
block_cipher = None
ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
SRC = os.path.join(ROOT, "src")
hidden = ["keyring.backends", "mutagen", "PIL.Image"]

# Only QtCore/QtGui/QtWidgets are used; leaving out the rest keeps the bundle small.
QT_UNUSED = ["PySide6." + m for m in ("QtQml", "QtQuick", "QtQuickWidgets", "QtPdf", "QtPdfWidgets", "QtNetwork",
             "QtWebEngineCore", "QtWebEngineWidgets", "QtMultimedia", "Qt3DCore", "QtCharts", "QtSql",
             "QtOpenGL", "QtOpenGLWidgets", "QtSvg", "QtDBus")]
RES = os.path.join(SRC, "datacurator", "gui", "resources")
gui = Analysis([os.path.join(ROOT, "packaging", "run_gui.py")], pathex=[SRC], hiddenimports=hidden,
               datas=[(RES, "datacurator/gui/resources")],
               excludes=["tkinter", "pytest", "numpy"] + QT_UNUSED, cipher=block_cipher)
# drop bundled Qt libraries for modules we excluded (PyInstaller may still collect them as plugin deps)
_drop = ("libQt6Qml", "libQt6Quick", "libQt6Pdf", "libQt6QmlModels", "libQt6VirtualKeyboard", "libQt6WebEngine")
gui.binaries = [b for b in gui.binaries if not any(os.path.basename(b[0]).startswith(d) for d in _drop)]
gui.datas = [d for d in gui.datas if "/translations/" not in d[0].replace("\\", "/") or "qtbase_" in d[0]]
cli = Analysis([os.path.join(ROOT, "packaging", "run_cli.py")], pathex=[SRC], hiddenimports=hidden,
               excludes=["tkinter", "pytest", "PySide6", "numpy"], cipher=block_cipher)
MERGE((gui, "run_gui", "DataCurator"), (cli, "run_cli", "curator"))

gui_pyz = PYZ(gui.pure, gui.zipped_data, cipher=block_cipher)
cli_pyz = PYZ(cli.pure, cli.zipped_data, cipher=block_cipher)
gui_exe = EXE(gui_pyz, gui.scripts, [], exclude_binaries=True, name="DataCurator", console=False,
              icon=os.path.join(RES, "AppIcon.ico") if sys.platform == "win32" else None)
cli_exe = EXE(cli_pyz, cli.scripts, [], exclude_binaries=True, name="curator", console=True)
coll = COLLECT(gui_exe, gui.binaries, gui.zipfiles, gui.datas, cli_exe, cli.binaries, cli.zipfiles, cli.datas,
               name="DataCurator")
if sys.platform == "darwin":
    app = BUNDLE(coll, name="Data Curator.app", bundle_identifier="org.datacurator.app",
                 icon=os.path.join(RES, "AppIcon.icns"),
                 info_plist={"NSHighResolutionCapable": True, "LSMinimumSystemVersion": "11.0"})
