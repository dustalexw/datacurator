"""Data Curator desktop application (PySide6 / Qt 6).

Workflow: Sources → Rules → Review → Results. The window only drives the shared
engine; it never touches files itself.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time

from PySide6.QtCore import QEvent, QSettings, QSignalBlocker, Qt, QTimer
from PySide6.QtGui import QAction, QFont, QKeySequence, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter, QStackedWidget,
                               QStatusBar, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser,
                               QTreeView, QVBoxLayout, QWidget)

from .. import __version__
from .. import profiles as P
from ..engine.api import Engine
from ..engine.executor import fmt_bytes
from ..engine.planstore import explain
from ..engine.music import save_details
from ..models import CuratorError, PreflightError
from ..naming.template import TOKENS_BY_CATEGORY, Template
from ..storage import locations as L
from . import workers as W
from . import macchrome
from . import theme as T
from .models import PlanTableModel, build_tree
from .about import AboutDialog
from .music import MusicPage
from .organization import ExtensionFolders, Exclusions, ScanDetailsDialog, VideoMetadata, DuplicatePage

CATS = [("music", "Music"), ("tv", "TV"), ("movies", "Movies"), ("documents", "Documents"), ("photos", "Photos"),
        ("other", "Other files")]
MODES = [("move_and_rename", "Sort into folders and rename"), ("move", "Sort into folders only"),
         ("rename", "Rename in place only")]
FILTERS = [("changes", "Proposed changes"), ("review", "Review queue"), ("conflicts", "Conflicts"),
           ("uncertain", "Uncertain matches"), ("duplicates", "Duplicates"), ("errors", "Errors"),
           ("sanitized", "Name adjustments"), ("limitations", "Preservation notes"), ("unchanged", "Already organized"),
           ("protected", "Protected (stays in place)"), ("skipped", "Left in place"), ("all", "Everything")]
SAMPLE = {
    "music": {"album_artist": "Daft Punk", "artist": "Daft Punk feat. Pharrell Williams", "album": "Random Access Memories",
              "year": 2013, "track": 8, "title": "Get Lucky", "format": "FLAC", "genre": "Electronic"},
    "tv": {"series": "The Expanse", "series_year": 2015, "season": 1, "episode": "2", "episode_code": "S01E02",
           "episode_title": "The Big Empty", "resolution": "1080p", "source": "WEB-DL", "codec": "x265"},
    "movies": {"title": "Arrival", "year": 2016, "resolution": "2160p"},
    "documents": {"project": "Taxes 2025", "name": "receipt-0412"},
    "photos": {"year": 2024, "month": 7, "day": 4, "name": "IMG_0412", "camera_make": "Canon"},
    "other": {"name": "setup", "type": "DMG", "group": "Installers", "project": "New app"},
}
SAMPLE_EXT = {"music": ".flac", "tv": ".mkv", "movies": ".mkv", "documents": ".pdf", "photos": ".jpg", "other": ".dmg"}

STYLE = """
QWidget { font-size: 10pt; }
QLabel[role="title"] { font-size: 15pt; font-weight: 600; }
QLabel[role="hint"] { color: palette(placeholder-text); }
QLabel[role="example"] { font-family: monospace; padding: 6px; border: 1px solid palette(mid); border-radius: 4px; }
QListWidget#steps { border: none; background: transparent; outline: 0; }
QListWidget#steps::item { padding: 10px 14px; border-radius: 6px; }
QListWidget#steps::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QPushButton { padding: 5px 12px; }
QPushButton[primary="true"] { font-weight: 600; padding: 6px 18px; }
QGroupBox { font-weight: 600; margin-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 4px; padding: 0 4px; }
"""


def label(text: str, role: str | None = None, wrap: bool = True) -> QLabel:
    l = QLabel(text)
    if role:
        l.setProperty("role", role)
    l.setWordWrap(wrap)
    return l


def primary(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setProperty("primary", True)
    b.setDefault(False)
    return b


# ===================================================================== category rules editor

class CategoryEditor(QWidget):
    def __init__(self, cat: str, on_change):
        super().__init__()
        self.cat = cat
        self.on_change = on_change
        lay = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        self.enabled = QCheckBox(f"Organize {dict(CATS)[cat].lower()}")
        self.enabled.setAccessibleName(f"Organize {cat}")
        form.addRow(self.enabled)
        self.preset = QComboBox()
        self.preset.setAccessibleName(f"{cat} layout preset")
        for key, pr in P.PRESETS[cat].items():
            self.preset.addItem(pr["label"], key)
        self.preset.addItem("Custom", "custom")
        form.addRow("Layout", self.preset)
        self.mode = QComboBox()
        self.mode.setAccessibleName(f"{cat} mode")
        for key, text in MODES:
            self.mode.addItem(text, key)
        form.addRow("What to do", self.mode)
        self.folder = QLineEdit()
        self.folder.setAccessibleName(f"{cat} folder template")
        self.filename = QLineEdit()
        self.filename.setAccessibleName(f"{cat} filename template")
        form.addRow("Folder template", self.folder)
        form.addRow("Filename template", self.filename)
        self.example = label("", "example")
        self.example.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.example.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.example.setAccessibleName(f"{cat} template example")
        form.addRow("Example", self.example)
        tokens = " ".join("{" + t + "}" for t in sorted(TOKENS_BY_CATEGORY[cat]) if t != "extension")
        hint = label(f"Tokens: {tokens}. Wrap optional parts in [ ], e.g. [{'{'}year{'}'} - ]. "
                     "Punctuation next to missing values disappears.", "hint")
        hint.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        form.addRow("", hint)
        self.extra: dict[str, QWidget] = {}
        if cat == "music":
            self.extra["compilation_name"] = QLineEdit()
            form.addRow("Compilations folder", self.extra["compilation_name"])
            self.extra["singles_folder"] = QLineEdit()
            self.extra["singles_folder"].setPlaceholderText("empty: tracks without an album wait for review")
            form.addRow("Singles folder", self.extra["singles_folder"])
            self.extra["path_fallback"] = QCheckBox("Suggest values from folder names for untagged files (needs review)")
            form.addRow("", self.extra["path_fallback"])
        if cat == "tv":
            cb = QComboBox()
            cb.addItem("Season/episode numbers (S01E02, 1x02, dates)", "auto")
            cb.addItem("Absolute episode numbers (Show - 123)", "absolute")
            self.extra["numbering"] = cb
            form.addRow("Numbering", cb)
        if cat == "photos":
            cb = QComboBox()
            cb.addItem("Use camera date only (others go to review)", "none")
            cb.addItem("Fall back to file modified time", "mtime")
            self.extra["date_fallback"] = cb
            form.addRow("Date", cb)
        lay.addLayout(form)
        lay.addStretch(1)
        self.preset.currentIndexChanged.connect(self._preset_changed)
        for w in (self.folder, self.filename):
            w.textChanged.connect(self._update_example)
        for w in (self.enabled, self.mode, *self.extra.values()):
            sig = getattr(w, "toggled", None) or getattr(w, "currentIndexChanged", None) or getattr(w, "textChanged")
            sig.connect(lambda *_: self.on_change())
        self._loading = False

    def _preset_changed(self):
        if self._loading:
            return
        key = self.preset.currentData()
        if key != "custom":
            pr = P.PRESETS[self.cat][key]
            self.folder.setText(pr["folder"])
            self.filename.setText(pr["filename"])

    def _update_example(self):
        vals = dict(SAMPLE[self.cat])
        ext = SAMPLE_EXT[self.cat]
        vals.update(ext=ext, extension=ext)
        try:
            ft = Template(self.folder.text(), "folder", self.cat)
            nt = Template(self.filename.text(), "filename", self.cat)
            full = "/".join(x for x in (ft.example(vals), nt.example(vals, ext)) if x)
            fewer = {k: v for k, v in vals.items() if k not in ("year", "track", "episode_title", "project", "month")}
            part = "/".join(x for x in (ft.example(fewer), nt.example(fewer, ext)) if x)
            self.example.setText(f"{full}\nwith missing values: {part}")
            self.example.setToolTip("")
        except CuratorError as e:
            self.example.setText(f"⚠ {e}")
        if not self._loading:
            match = next((k for k, pr in P.PRESETS[self.cat].items()
                          if pr["folder"] == self.folder.text() and pr["filename"] == self.filename.text()), "custom")
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(self.preset.findData(match))
            self.preset.blockSignals(False)
            self.on_change()

    def load(self, cfg: dict):
        self._loading = True
        self.enabled.setChecked(bool(cfg.get("enabled")))
        self.mode.setCurrentIndex(max(0, self.mode.findData(cfg.get("mode", "move_and_rename"))))
        self.folder.setText(cfg.get("folder", ""))
        self.filename.setText(cfg.get("filename", ""))
        match = next((k for k, pr in P.PRESETS[self.cat].items()
                      if pr["folder"] == cfg.get("folder") and pr["filename"] == cfg.get("filename")), "custom")
        self.preset.setCurrentIndex(self.preset.findData(match))
        for k, w in self.extra.items():
            v = cfg.get(k)
            if isinstance(w, QLineEdit):
                w.setText(v or "")
            elif isinstance(w, QCheckBox):
                w.setChecked(bool(v if v is not None else True))
            elif isinstance(w, QComboBox):
                w.setCurrentIndex(max(0, w.findData(v)))
        self._loading = False
        self._update_example()

    def dump(self, cfg: dict) -> dict:
        cfg = dict(cfg)
        cfg.update(enabled=self.enabled.isChecked(), mode=self.mode.currentData(), folder=self.folder.text(),
                   filename=self.filename.text())
        if self.preset.currentData() != "custom":
            cfg["preset"] = self.preset.currentData()
        for k, w in self.extra.items():
            if isinstance(w, QLineEdit):
                cfg[k] = w.text()
            elif isinstance(w, QCheckBox):
                cfg[k] = w.isChecked()
            elif isinstance(w, QComboBox):
                cfg[k] = w.currentData()
        return cfg


# ===================================================================== override dialog

class OverrideDialog(QDialog):
    FIELDS = {"music": ["album_artist", "album", "artist", "title", "track", "disc", "year", "compilation"],
              "tv": ["series", "series_year", "season", "episode", "episode_title", "absolute", "series_id"],
              "movies": ["title", "year", "edition", "movie_id"], "photos": ["year", "month"],
              "documents": ["project", "name"], "other": ["name"]}

    def __init__(self, parent, category: str, n: int, groups: bool):
        super().__init__(parent)
        self.setWindowTitle("Set a value")
        lay = QFormLayout(self)
        lay.addRow(label(f"This changes how {n} selected file(s) are organized. It is stored in the catalog only; "
                         "file contents and tags are not modified unless you enable tag writing.", "hint"))
        self.field = QComboBox()
        for f in self.FIELDS.get(category, ["name"]) + ["category"]:
            self.field.addItem(f)
        self.field.setEditable(True)
        lay.addRow("Field", self.field)
        self.value = QLineEdit()
        self.value.setPlaceholderText("value (empty removes the override)")
        lay.addRow("Value", self.value)
        self.scope = QCheckBox("Apply to the whole album / series of each selected file")
        self.scope.setChecked(groups)
        lay.addRow("", self.scope)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addRow(bb)


# ===================================================================== main window

class MainWindow(QMainWindow):
    def __init__(self, engine: Engine | None = None):
        super().__init__()
        self.engine = engine or Engine()
        self.setWindowTitle("Data Curator")
        self.resize(1280, 820)
        self.settings = QSettings("DataCurator", "DataCurator")
        self.profile_path: str | None = None
        self.profile: dict = P.opening_profile()
        self.store = None
        self.thread = None
        self.worker = None
        self.last_batch = None
        self.profiles_dir = os.path.join(self.engine.data_dir, "profiles")
        os.makedirs(self.profiles_dir, exist_ok=True)

        self.setWindowIcon(T.app_icon())
        macchrome.prepare(self)
        central = QWidget()
        central.setObjectName("Root")
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(232)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(16, 18, 16, 14)
        self._side_layout = sl
        self._chrome_inset = 0
        brand = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(T.logo_pixmap(46))
        logo.setFixedSize(48, 48)
        logo.setAccessibleName("Data Curator logo")
        brand.addWidget(logo)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(T.BrandTitle("Data Curator", 14.5))
        tag = label("preview · apply · undo", "hint", wrap=False)
        tag.setStyleSheet("font-size: 8.5pt;")
        names.addWidget(tag)
        brand.addLayout(names, 1)
        sl.addLayout(brand)
        sl.addSpacing(12)
        sl.addWidget(T.PeppermintStripe())
        sl.addSpacing(12)
        sl.addWidget(label("WORKFLOW", "section", wrap=False))
        sl.addSpacing(4)
        self.steps = QListWidget()
        self.steps.setObjectName("steps")
        self.steps.setAccessibleName("Workflow steps")
        for t in ("①  Sources", "②  Rules", "③  Review", "④  Results", "♫  Music library", "⊜  Duplicates"):
            self.steps.addItem(QListWidgetItem(t))
        sl.addWidget(self.steps, 1)
        safe = QGroupBox()
        safe_l = QVBoxLayout(safe)
        safe_l.setContentsMargins(4, 0, 4, 0)
        safe_l.addWidget(label("🛡  Safe by design", None, wrap=False))
        safe_l.addWidget(label("Previews never change your files. Every step is journaled and can be undone.",
                               "hint"))
        sl.addWidget(safe)
        sl.addWidget(label(f"v{__version__}", "hint", wrap=False))
        root.addWidget(side)
        self.pages = QStackedWidget()
        root.addWidget(self.pages, 1)
        self.setCentralWidget(central)
        self.pages.addWidget(self._page_sources())
        self.pages.addWidget(self._page_rules())
        self.pages.addWidget(self._page_review())
        self.pages.addWidget(self._page_results())
        self.music_page = MusicPage(self, self.scan_music_library, self.save_music_details,
                                    self.use_music_layout, self.scan_and_preview)
        self.pages.addWidget(self.music_page)
        self.duplicate_page = DuplicatePage(self, self.find_duplicates, self.quarantine_duplicates, self._rules_changed)
        self.pages.addWidget(self.duplicate_page)
        self.music_page.write_tags.toggled.connect(self.opt_tags.setChecked)
        self.opt_tags.toggled.connect(self.music_page.write_tags.setChecked)
        self.steps.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.steps.setCurrentRow(0)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(280)
        self.progress.setVisible(False)
        self.progress.setAccessibleName("Progress")
        self.status_text = QLabel("Ready")
        self.pause_btn = QPushButton("Pause")
        self.cancel_btn = QPushButton("Cancel")
        for w in (self.pause_btn, self.cancel_btn):
            w.setVisible(False)
        self.pause_btn.clicked.connect(self._toggle_pause)
        self.cancel_btn.clicked.connect(self._cancel)
        sb.addWidget(self.status_text, 1)
        sb.addPermanentWidget(self.progress)
        sb.addPermanentWidget(self.pause_btn)
        sb.addPermanentWidget(self.cancel_btn)
        self._menus()
        self.sources.model().rowsInserted.connect(lambda *_: self._refresh_destinations())
        self.sources.model().rowsRemoved.connect(lambda *_: self._refresh_destinations())
        last = self.settings.value("last_profile")
        if last and os.path.exists(str(last)):
            self._load_profile_file(str(last))
        else:
            self._load_profile_into_ui(self.profile)
        QTimer.singleShot(200, self._check_interrupted)

    # ------------------------------------------------------------------ menus
    def _menus(self):
        if sys.platform == "darwin":
            self.menuBar().setNativeMenuBar(True)
        m = self.menuBar().addMenu("&File")
        for text, key, fn in (("Open profile…", QKeySequence.Open, self.open_profile),
                              ("Save profile", QKeySequence.Save, self.save_profile),
                              ("Save profile as…", QKeySequence.SaveAs, self.save_profile_as),
                              ("Export report…", "Ctrl+E", self.export_report)):
            a = QAction(text, self)
            a.setShortcut(key)
            a.triggered.connect(fn)
            m.addAction(a)
        m.addSeparator()
        q = QAction("Quit", self)
        q.setMenuRole(QAction.QuitRole)
        q.setShortcut(QKeySequence.Quit)
        q.triggered.connect(self.close)
        m.addAction(q)
        v = self.menuBar().addMenu("&View")
        from PySide6.QtGui import QActionGroup
        grp = QActionGroup(self)
        self.theme_actions = {}
        for key, text in (("light", "Peppermint light"), ("dark", "Peppermint dark — burgundy"), ("system", "System theme")):
            a = QAction(text, self, checkable=True)
            a.setChecked(T.normalize_name(self.settings.value("theme") or "dark") == key)
            a.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            grp.addAction(a)
            v.addAction(a)
            self.theme_actions[key] = a
        h = self.menuBar().addMenu("&Help")
        self.about_action = QAction("About Data Curator…", self)
        self.about_action.setMenuRole(QAction.AboutRole)
        self.about_action.triggered.connect(self.show_about)
        h.addAction(self.about_action)
        music = self.menuBar().addMenu("&Music")
        browse = music.addAction("Browse music library")
        browse.setShortcut("Ctrl+M")
        browse.triggered.connect(lambda: self.steps.setCurrentRow(4))
        music.addAction("Scan music library", self.scan_music_library)
        tools = self.menuBar().addMenu("&Tools")
        tools.addAction("Find duplicates", lambda: self.steps.setCurrentRow(5))

    def show_about(self):
        AboutDialog(self).exec()

    # ------------------------------------------------------------ title bar
    def _apply_chrome(self):
        """Run the sidebar and page colours up under a transparent macOS title bar."""
        on = macchrome.integrate(self, self.palette().color(QPalette.Window))
        inset = macchrome.top_inset(self, macchrome.TITLEBAR_HEIGHT) if on and not self.isFullScreen() else 0
        if inset != self._chrome_inset:
            self._chrome_inset = inset
            self._side_layout.setContentsMargins(16, 18 + inset, 16, 14)
            self.pages.setContentsMargins(0, inset, 0, 0)

    def showEvent(self, ev):
        super().showEvent(ev)
        self._apply_chrome()
        handle = self.windowHandle()
        if handle is not None and hasattr(handle, "safeAreaMarginsChanged") and not getattr(self, "_watching_safe_area", False):
            self._watching_safe_area = True
            handle.safeAreaMarginsChanged.connect(lambda *_: self._apply_chrome())

    def changeEvent(self, ev):
        super().changeEvent(ev)
        if ev.type() == QEvent.WindowStateChange:
            QTimer.singleShot(0, self._apply_chrome)

    def mousePressEvent(self, ev):
        # The title bar strip drags the window even where Qt, not macOS, gets the click.
        handle = self.windowHandle()
        if ev.button() == Qt.LeftButton and ev.position().y() < self._chrome_inset and handle:
            handle.startSystemMove()
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseDoubleClickEvent(self, ev):
        if ev.button() == Qt.LeftButton and ev.position().y() < self._chrome_inset:
            self.showNormal() if self.isMaximized() else self.showMaximized()
            ev.accept()
            return
        super().mouseDoubleClickEvent(ev)

    def set_theme(self, name: str):
        name = T.apply(QApplication.instance(), name)
        self._apply_chrome()
        self.settings.setValue("theme", name)
        for key, action in self.theme_actions.items():
            action.setChecked(key == name)
        self.refresh_history()
        for t in list(getattr(self, "tiles", {}).values()) + list(getattr(self, "run_tiles", {}).values()):
            t.set_active(t.key == self.filter.currentData() if t in self.tiles.values() else False)
        self.update()

    # ------------------------------------------------------------------ page 1
    def _page_sources(self) -> QWidget:
        w = QWidget()
        w.setObjectName("Page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(10)
        lay.addWidget(T.BrandTitle("Sources and destinations", 20))
        lay.addWidget(label("Choose your source folders. By default, each category goes into its own named folder "
                            "inside each source. Browse to choose a different destination.",
                            "hint"))
        prof_row = QHBoxLayout()
        prof_row.addWidget(QLabel("Profile:"))
        self.profile_label = QLabel("Unsaved profile")
        prof_row.addWidget(self.profile_label, 1)
        for text, fn in (("Open…", self.open_profile), ("Save", self.save_profile), ("Save as…", self.save_profile_as)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            prof_row.addWidget(b)
        lay.addLayout(prof_row)

        g = QGroupBox("Sources")
        gl = QVBoxLayout(g)
        self.sources = QListWidget()
        self.sources.setAccessibleName("Source folders")
        self.sources.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sources.setMaximumHeight(130)
        gl.addWidget(self.sources)
        row = QHBoxLayout()
        add = QPushButton("Add folder…")
        add.clicked.connect(self.add_source)
        rem = QPushButton("Remove")
        rem.clicked.connect(self.remove_source)
        self.locations_combo = QComboBox()
        self.locations_combo.setAccessibleName("Saved locations")
        self.locations_combo.setMinimumWidth(260)
        addloc = QPushButton("Add saved location")
        addloc.clicked.connect(self.add_saved_location_source)
        saveloc = QPushButton("Save as location…")
        saveloc.clicked.connect(self.save_location)
        for x in (add, rem):
            row.addWidget(x)
        row.addStretch(1)
        row.addWidget(self.locations_combo)
        row.addWidget(addloc)
        row.addWidget(saveloc)
        gl.addLayout(row)
        self.location_status = label("", "hint")
        gl.addWidget(self.location_status)
        lay.addWidget(g)

        self.opt_all_files = QCheckBox("Detect all files, including hidden files and files already in destination folders")
        self.opt_all_files.setToolTip("Scans subfolders and mounted folders too. Explicit exclusions, links and app working files are reported separately. Protected game and application files are listed but left in place.")
        self.opt_all_files.toggled.connect(self._rules_changed)
        lay.addWidget(self.opt_all_files)

        g = QGroupBox("Destinations")
        grid = QGridLayout(g)
        self.dest_edits: dict[str, QLineEdit] = {}
        self.dest_info: dict[str, QLabel] = {}
        self.dest_in_source: dict[str, QCheckBox] = {}
        for i, (cat, title) in enumerate(CATS):
            grid.addWidget(QLabel(title), i, 0)
            e = QLineEdit()
            e.setPlaceholderText("organize inside the source folder")
            e.setAccessibleName(f"{title} destination folder")
            self.dest_edits[cat] = e
            e.textChanged.connect(lambda text, c=cat: self._destination_changed(c, text))
            grid.addWidget(e, i, 1)
            inside = QCheckBox("Inside source")
            inside.setAccessibleName(f"{title} inside each source")
            self.dest_in_source[cat] = inside
            inside.toggled.connect(lambda checked, c=cat: self._inside_source_changed(c, checked))
            grid.addWidget(inside, i, 2)
            b = QPushButton("Browse…")
            b.setAccessibleName(f"Browse for {title} destination")
            b.clicked.connect(lambda _=False, c=cat: self.browse_dest(c))
            grid.addWidget(b, i, 3)
            chk = QPushButton("Check")
            chk.setToolTip("Measure this destination: case sensitivity, Unicode, name rules, links, free space")
            chk.setAccessibleName(f"Check {title} destination")
            chk.clicked.connect(lambda _=False, c=cat: self.check_dest(c))
            grid.addWidget(chk, i, 4)
            info = label("", "hint")
            self.dest_info[cat] = info
            grid.addWidget(info, i, 5)
            grid.setColumnStretch(1, 3)
            grid.setColumnStretch(5, 2)
        lay.addWidget(g)
        lay.addStretch(1)
        nav = QHBoxLayout()
        nav.addStretch(1)
        category_defaults = QPushButton("Use category folders in sources")
        category_defaults.clicked.connect(self.use_category_layout)
        nav.addWidget(category_defaults)
        quick = QPushButton("Mixed files: subfolders inside sources")
        quick.setToolTip("Use format folders for music, videos, photos, documents and archives inside each source. Preview before applying.")
        quick.clicked.connect(self.use_mixed_layout)
        nav.addWidget(quick)
        nxt = primary("Next: rules →")
        nxt.clicked.connect(lambda: self.steps.setCurrentRow(1))
        nav.addWidget(nxt)
        lay.addLayout(nav)
        self._refresh_locations()
        return w

    # ------------------------------------------------------------------ page 2
    def _page_rules(self) -> QWidget:
        w = QWidget()
        w.setObjectName("Page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(10)
        lay.addWidget(T.BrandTitle("Organization rules", 20))
        lay.addWidget(label("Pick a layout per kind of file. Only folders the layout needs are created; tags, "
                            "collections and the review queue live in the catalog, not in extra folders.", "hint"))
        self.cat_tabs = QTabWidget()
        self.editors: dict[str, CategoryEditor] = {}
        for cat, title in CATS:
            ed = CategoryEditor(cat, self._rules_changed)
            self.editors[cat] = ed
            self.cat_tabs.addTab(ed, title)
        self.extension_rules = ExtensionFolders(self._rules_changed)
        self.exclusions = Exclusions(self._rules_changed)
        self.video_metadata = VideoMetadata(self._rules_changed)
        self.provider = self.video_metadata.tv
        self.cat_tabs.addTab(self.extension_rules, "Extensions")
        self.cat_tabs.addTab(self.exclusions, "Exclusions")
        self.cat_tabs.addTab(self.video_metadata, "Video metadata")
        lay.addWidget(self.cat_tabs, 3)

        g = QGroupBox("When something needs a decision")
        f = QFormLayout(g)
        self.conflict = QComboBox()
        self.conflict.setAccessibleName("Name collision handling")
        for k, t in (("review", "Keep both files in place and ask me (review)"),
                     ("alternate", "Add a number to the new name, e.g. “Song (2).flac”"),
                     ("skip", "Skip the file")):
            self.conflict.addItem(t, k)
        f.addRow("Name collisions", self.conflict)
        self.naming = QComboBox()
        self.naming.addItem("Portable names (safe on Windows, macOS, Linux, NAS)", "portable")
        self.naming.addItem("Destination's own rules", "native")
        f.addRow("File names", self.naming)
        lay.addWidget(g)

        self.adv = QGroupBox("Advanced")
        self.adv.setCheckable(True)
        self.adv.setChecked(False)
        al = QGridLayout(self.adv)
        self.opt_refs = QCheckBox("Update references in cue sheets and playlists (originals kept for undo)")
        self.opt_tags = QCheckBox("Write my overrides into embedded tags (originals kept for undo)")
        self.opt_dupes = QCheckBox("Detect identical copies by content")
        self.opt_lossy = QCheckBox("Block transfers that cannot preserve permissions/attributes/links")
        self.opt_cleanup = QCheckBox("Remove source folders that become empty")
        self.opt_symlinks = QCheckBox("Follow folder symlinks while scanning")
        self.opt_mounts = QCheckBox("Scan into other drives mounted inside sources")
        self.opt_hidden = QCheckBox("Include hidden files")
        opts = [self.opt_refs, self.opt_tags, self.opt_dupes, self.opt_lossy, self.opt_cleanup, self.opt_symlinks,
                self.opt_mounts, self.opt_hidden]
        for i, c in enumerate(opts):
            al.addWidget(c, i // 2, i % 2)
            c.toggled.connect(lambda *_: self._rules_changed())
        hidden = opts
        self.adv.toggled.connect(lambda on: [c.setVisible(on) for c in hidden])
        for c in hidden:
            c.setVisible(False)
        lay.addWidget(self.adv)
        nav = QHBoxLayout()
        back = QPushButton("← Back")
        back.clicked.connect(lambda: self.steps.setCurrentRow(0))
        nav.addWidget(back)
        nav.addStretch(1)
        self.scan_btn = primary("Scan and preview")
        self.scan_btn.setShortcut("Ctrl+R")
        self.scan_btn.setToolTip("Reads your files and proposes changes. Nothing is changed. (Ctrl+R)")
        self.scan_btn.clicked.connect(self.scan_and_preview)
        nav.addWidget(self.scan_btn)
        lay.addLayout(nav)
        return w

    # ------------------------------------------------------------------ page 3
    def _page_review(self) -> QWidget:
        w = QWidget()
        w.setObjectName("Page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(10)
        lay.addWidget(T.BrandTitle("Review proposed changes", 20))
        self.summary = label("Run “Scan and preview” to see proposed changes.", "hint")
        self.summary.setAccessibleName("Plan summary")
        lay.addWidget(self.summary)
        tiles = QHBoxLayout()
        tiles.setSpacing(10)
        self.tiles: dict[str, T.StatTile] = {}
        for key, text, color in (("changes", "To change", T.ACCENT), ("review", "Review queue", T.WARNING),
                                 ("conflicts", "Conflicts", T.ERROR), ("duplicates", "Duplicates", T.DUPLICATE),
                                 ("unchanged", "Already organized", T.SUCCESS), ("_folders", "New folders", T.INFO),
                                 ("_bytes", "Copy between drives", T.QUARANTINE)):
            t = T.StatTile(key, text, color)
            t.clicked.connect(self._tile_clicked)
            self.tiles[key] = t
            tiles.addWidget(t)
        lay.addLayout(tiles)
        bar = QHBoxLayout()
        self.filter = QComboBox()
        self.filter.setAccessibleName("Filter")
        for k, t in FILTERS:
            self.filter.addItem(t, k)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search paths and reasons  (Ctrl+F)")
        self.search.setAccessibleName("Search")
        self.search.setClearButtonEnabled(True)
        sa = QAction(self)
        sa.setShortcut("Ctrl+F")
        sa.triggered.connect(lambda: self.search.setFocus())
        self.addAction(sa)
        bar.addWidget(self.filter)
        bar.addWidget(self.search, 1)
        self.scan_details_btn = QPushButton("Scan details…")
        self.scan_details_btn.clicked.connect(self.show_scan_details)
        self.scan_details_btn.hide()
        self._scan_details = ""
        bar.addWidget(self.scan_details_btn)
        lay.addLayout(bar)
        self.scan_notice = label("", "hint")
        self.scan_notice.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.scan_notice.hide()
        lay.addWidget(self.scan_notice)
        split = QSplitter(Qt.Horizontal)
        self.table = QTableView()
        self.table.setAccessibleName("Proposed changes")
        self.model = PlanTableModel(self)
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.setItemDelegateForColumn(0, T.ActionDelegate(self.table))
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        for i, wdt in enumerate((190, 150, 320, 360, 280)):
            self.table.setColumnWidth(i, wdt)
        hh.setStretchLastSection(True)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        split.addWidget(self.table)
        right = QTabWidget()
        self.details = QTextBrowser()
        self.details.setAccessibleName("Details of the selected file")
        self.details.setOpenLinks(False)
        right.addTab(self.details, "Details")
        self.tree = QTreeView()
        self.tree.setAccessibleName("Proposed folder tree")
        self.tree.setUniformRowHeights(True)
        right.addTab(self.tree, "Proposed folders")
        split.addWidget(right)
        split.setSizes([900, 380])
        lay.addWidget(split, 1)
        acts = QHBoxLayout()
        self.btn_exclude = QPushButton("Leave alone")
        self.btn_exclude.setToolTip("Exclude selected files from organizing (kept in the catalog)")
        self.btn_include = QPushButton("Include again")
        self.btn_accept = QPushButton("Accept suggestion")
        self.btn_override = QPushButton("Set value…")
        self.btn_match = QPushButton("Choose match…")
        self.btn_replan = QPushButton("Update preview")
        self.btn_replan.setToolTip("Re-plan with your changes (does not rescan unless files changed)")
        for b, fn in ((self.btn_exclude, self.exclude_selected), (self.btn_include, self.include_selected),
                      (self.btn_accept, self.accept_selected), (self.btn_override, self.override_selected),
                      (self.btn_match, self.choose_match), (self.btn_replan, self.replan)):
            b.clicked.connect(fn)
            acts.addWidget(b)
        acts.addStretch(1)
        exp = QPushButton("Export report…")
        exp.clicked.connect(self.export_report)
        acts.addWidget(exp)
        self.apply_btn = primary("Apply changes…")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        acts.addWidget(self.apply_btn)
        lay.addLayout(acts)
        self.filter.currentIndexChanged.connect(self._filter_changed)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(250)
        self._search_timer.timeout.connect(self._filter_changed)
        self.search.textChanged.connect(lambda *_: self._search_timer.start())
        return w

    # ------------------------------------------------------------------ page 4
    def _page_results(self) -> QWidget:
        w = QWidget()
        w.setObjectName("Page")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.setSpacing(10)
        lay.addWidget(T.BrandTitle("Results and history", 20))
        rt = QHBoxLayout()
        rt.setSpacing(10)
        self.run_tiles: dict[str, T.StatTile] = {}
        for key, text, color in (("done", "Done", T.SUCCESS), ("failed", "Failed", T.ERROR),
                                 ("skipped", "Left in place", T.WARNING), ("bytes", "Copied & verified", T.ACCENT)):
            t = T.StatTile(key, text, color)
            self.run_tiles[key] = t
            rt.addWidget(t)
        lay.addLayout(rt)
        self.result_text = QTextBrowser()
        self.result_text.setAccessibleName("Result of the last run")
        self.result_text.setMaximumHeight(180)
        lay.addWidget(self.result_text)
        lay.addWidget(label("Every run is recorded in the recovery journal. Undo checks that files are still the "
                            "ones that were organized and never overwrites anything.", "hint"))
        self.history = QTableWidget(0, 5)
        self.history.setAccessibleName("Run history")
        self.history.setHorizontalHeaderLabels(["Run", "When", "Kind", "Status", "Summary"])
        self.history.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history.setSelectionMode(QAbstractItemView.SingleSelection)
        self.history.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history.verticalHeader().setVisible(False)
        self.history.horizontalHeader().setStretchLastSection(True)
        for i, wd in enumerate((190, 140, 70, 150)):
            self.history.setColumnWidth(i, wd)
        lay.addWidget(self.history, 1)
        row = QHBoxLayout()
        self.undo_btn = QPushButton("Undo selected run…")
        self.undo_btn.clicked.connect(self.undo_selected)
        self.recover_btn = QPushButton("Recover interrupted run…")
        self.recover_btn.clicked.connect(lambda: self._check_interrupted(force=True))
        ref = QPushButton("Refresh")
        ref.clicked.connect(self.refresh_history)
        for b in (self.undo_btn, self.recover_btn, ref):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        self.refresh_history()
        return w

    # ================================================================== profile <-> UI
    def _load_profile_into_ui(self, prof: dict):
        self.profile = prof
        self.sources.clear()
        for s in prof["sources"]:
            self.sources.addItem(s)
        for cat, _ in CATS:
            cfg = prof["categories"][cat]
            # Loading a saved profile must preserve intentional disabled categories.
            with QSignalBlocker(self.dest_edits[cat]):
                self.dest_edits[cat].setText(cfg.get("root", ""))
            with QSignalBlocker(self.dest_in_source[cat]):
                self.dest_in_source[cat].setChecked(bool(cfg.get("source_subfolder")))
            self.editors[cat].load(cfg)
        self._refresh_destinations()
        self.opt_all_files.setChecked(prof["scan"].get("inventory_all", False))
        self.filter.setCurrentIndex(self.filter.findData("all"))
        self.conflict.setCurrentIndex(max(0, self.conflict.findData(prof.get("conflict_policy"))))
        self.naming.setCurrentIndex(max(0, self.naming.findData(prof["naming"].get("preset"))))
        self.opt_refs.setChecked(prof["companions"].get("update_references", False))
        self.opt_tags.setChecked(bool(prof.get("write_tags")))
        self.opt_dupes.setChecked(prof.get("detect_duplicates", True))
        self.opt_lossy.setChecked(prof["preservation"].get("block_lossy", False))
        self.opt_cleanup.setChecked(bool(prof.get("cleanup_empty_dirs")))
        self.opt_symlinks.setChecked(prof["scan"].get("follow_symlinks", False))
        self.opt_mounts.setChecked(prof["scan"].get("cross_mounts", False))
        self.opt_hidden.setChecked(prof["scan"].get("include_hidden", False))
        self.provider.setCurrentIndex(max(0, self.provider.findData((prof.get("providers") or {}).get("tv", "none"))))
        self.profile_label.setText(self.profile_path or "Unsaved profile")
        self.music_page.set_layout(prof["categories"]["music"])
        self.music_page.set_rows([])
        self.extension_rules.load(prof["extension_folders"])
        self.exclusions.load(prof["scan"])
        self.video_metadata.load(prof["providers"])
        self.duplicate_page.load(prof["dedupe"])
        self._rules_changed()

    def current_profile(self) -> dict:
        p = copy.deepcopy(self.profile)
        p["sources"] = [self.sources.item(i).text() for i in range(self.sources.count())]
        for cat, _ in CATS:
            cfg = self.editors[cat].dump(p["categories"][cat])
            if self.dest_in_source[cat].isChecked():
                cfg["root"] = ""
                cfg["source_subfolder"] = cfg.get("source_subfolder") or P.CATEGORY_FOLDERS[cat]
            else:
                cfg["root"] = self.dest_edits[cat].text().strip()
                cfg.pop("source_subfolder", None)
            cfg.pop("preset", None) if self.editors[cat].preset.currentData() == "custom" else None
            p["categories"][cat] = cfg
        p["conflict_policy"] = self.conflict.currentData()
        p["naming"]["preset"] = self.naming.currentData()
        p["companions"]["update_references"] = self.opt_refs.isChecked()
        p["write_tags"] = self.opt_tags.isChecked()
        p["detect_duplicates"] = self.opt_dupes.isChecked()
        p["preservation"]["block_lossy"] = self.opt_lossy.isChecked()
        p["cleanup_empty_dirs"] = self.opt_cleanup.isChecked()
        p["scan"].update(follow_symlinks=self.opt_symlinks.isChecked(), cross_mounts=self.opt_mounts.isChecked(),
                         include_hidden=self.opt_hidden.isChecked(), inventory_all=self.opt_all_files.isChecked())
        p.setdefault("providers", {})["tv"] = self.provider.currentData()
        p["providers"]["movies"] = self.video_metadata.movies.currentData()
        p["extension_folders"] = self.extension_rules.dump()
        p["scan"].update(self.exclusions.dump())
        p["dedupe"].update(self.duplicate_page.dump())
        return p

    def _rules_changed(self, *_):
        if hasattr(self, "duplicate_page"):
            self.duplicate_page.invalidate()
        if self.store is not None:
            self.apply_btn.setEnabled(False)
            self.summary.setText("Rules changed — run “Scan and preview” again before applying.")

    def _destination_changed(self, cat, text):
        if self.dest_in_source[cat].isChecked():
            with QSignalBlocker(self.dest_in_source[cat]):
                self.dest_in_source[cat].setChecked(False)
            self.dest_edits[cat].setReadOnly(False)
        if text.strip():
            self.editors[cat].enabled.setChecked(True)
        self._rules_changed()

    def _refresh_destinations(self):
        sources = [self.sources.item(i).text() for i in range(self.sources.count())]
        for cat, edit in self.dest_edits.items():
            inside = self.dest_in_source[cat].isChecked()
            edit.setReadOnly(inside)
            if inside:
                folder = self.profile["categories"][cat].get("source_subfolder") or P.CATEGORY_FOLDERS[cat]
                path = os.path.join(sources[0], folder) if len(sources) == 1 else f"Each source / {folder}"
                with QSignalBlocker(edit):
                    edit.setText(path)
                edit.setToolTip("\n".join(os.path.join(source, folder) for source in sources) or "Choose a source folder first.")
        self._rules_changed()

    def _inside_source_changed(self, cat, checked):
        if checked:
            self.editors[cat].enabled.setChecked(True)
        if not checked:
            with QSignalBlocker(self.dest_edits[cat]):
                self.dest_edits[cat].setText(self.profile["categories"][cat].get("root", ""))
        self._refresh_destinations()

    # ================================================================== actions: sources
    def add_source(self):
        d = QFileDialog.getExistingDirectory(self, "Add a folder to organize")
        if d and not self.sources.findItems(d, Qt.MatchExactly):
            self.sources.addItem(d)
            self._rules_changed()

    def remove_source(self):
        for it in self.sources.selectedItems():
            self.sources.takeItem(self.sources.row(it))
        self._rules_changed()

    def browse_dest(self, cat):
        d = QFileDialog.getExistingDirectory(self, f"Destination for {dict(CATS)[cat]}")
        if d:
            self.dest_edits[cat].setText(d)
            self._destination_changed(cat, d)

    def check_dest(self, cat):
        if self.dest_in_source[cat].isChecked():
            path = P.destination_root(self.current_profile()["categories"][cat], self.sources.item(0).text()) if self.sources.count() else ""
        else:
            path = self.dest_edits[cat].text().strip() or (self.sources.item(0).text() if self.sources.count() else "")
        if not path:
            self.dest_info[cat].setText("Choose a folder first.")
            return
        self.dest_info[cat].setText("Checking…")

        def done(c):
            if c.probed == "active":
                self.engine.catalog.store_capabilities(os.path.abspath(path), c.to_dict())
            parts = [c.fs_type, "case-sensitive" if c.case_sensitive else "case-insensitive"]
            if c.windows_naming:
                parts.append("Windows name rules")
            if c.ascii_only:
                parts.append("ASCII names only")
            if c.is_network:
                parts.append("network")
            if c.read_only:
                parts.append("read-only")
            if c.free_bytes is not None:
                parts.append(f"{fmt_bytes(c.free_bytes)} free")
            self.dest_info[cat].setText(", ".join(parts))
            self.dest_info[cat].setToolTip("\n".join(c.notes) or "Measured by creating and removing a hidden test file.")
        self._run(W.job_probe, done, path, quiet=True)

    def _refresh_locations(self):
        self.locations_combo.clear()
        locs = self.engine.catalog.locations()
        if not locs:
            self.locations_combo.addItem("No saved locations", None)
            return
        for loc in locs:
            st = L.status(loc, timeout=2)
            mark = "●" if st.reachable and st.same_storage is not False else "○"
            self.locations_combo.addItem(f"{mark} {loc['name']} — {loc['path']}", loc)
            if not st.reachable:
                self.location_status.setText(f"{loc['name']}: {st.message} {st.guidance}")

    def add_saved_location_source(self):
        loc = self.locations_combo.currentData()
        if not loc:
            return
        st = L.status(loc, timeout=3)
        if not st.reachable:
            QMessageBox.information(self, "Location not connected", f"{st.message}\n\n{st.guidance}")
            return
        if not self.sources.findItems(loc["path"], Qt.MatchExactly):
            self.sources.addItem(loc["path"])

    def save_location(self):
        items = self.sources.selectedItems()
        if not items:
            QMessageBox.information(self, "Save location", "Select a source folder first.")
            return
        name, ok = QInputDialog.getText(self, "Save location", "Name for this location:")
        if ok and name:
            from ..storage.capabilities import volume_identity
            path = items[0].text()
            self.engine.catalog.save_location(name, path, identity=volume_identity(path).to_dict())
            self._refresh_locations()

    # ================================================================== profiles
    def open_profile(self):
        if self.thread is not None:
            QMessageBox.information(self, "Busy", "Wait for the current task before opening a profile.")
            return
        f, _ = QFileDialog.getOpenFileName(self, "Open profile", self.profiles_dir, "Profiles (*.json)")
        if f:
            self._load_profile_file(f)

    def _load_profile_file(self, f):
        try:
            prof = P.load(f)
        except (CuratorError, OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "Cannot open profile", str(e))
            return
        self.profile_path = f
        self.settings.setValue("last_profile", f)
        self._load_profile_into_ui(prof)

    def save_profile(self):
        if not self.profile_path:
            return self.save_profile_as()
        try:
            prof = P.normalize(self.current_profile())
        except CuratorError as e:
            QMessageBox.warning(self, "Cannot save", str(e))
            return
        P.save(prof, self.profile_path)
        self.settings.setValue("last_profile", self.profile_path)
        self.statusBar().showMessage("Profile saved", 3000)

    def save_profile_as(self):
        f, _ = QFileDialog.getSaveFileName(self, "Save profile", os.path.join(self.profiles_dir, "profile.json"),
                                           "Profiles (*.json)")
        if f:
            self.profile_path = f
            self.profile_label.setText(f)
            self.save_profile()

    # ================================================================== scan / plan
    def use_mixed_layout(self):
        if self.thread is not None:
            return
        sources = [self.sources.item(i).text() for i in range(self.sources.count())]
        prof = P.mixed_in_source_profile(sources)
        prof["scan"]["inventory_all"] = self.opt_all_files.isChecked()
        # Preserve folders the user explicitly asked to protect (or to organize anyway).
        prof["scan"].update(self.exclusions.dump())
        self.profile_path = None
        self._load_profile_into_ui(prof)
        self.steps.setCurrentRow(1)
        self.cat_tabs.setCurrentWidget(self.extension_rules)
        self.status_text.setText("Mixed-file layout selected. Scan and preview before applying.")

    def use_category_layout(self):
        sources = [self.sources.item(i).text() for i in range(self.sources.count())]
        self.profile_path = None
        prof = P.opening_profile(sources)
        prof["scan"].update(self.exclusions.dump())
        self._load_profile_into_ui(prof)
        self.status_text.setText("Category folders selected inside each source. Scan and preview before applying.")

    def find_duplicates(self):
        try:
            prof = P.normalize(self.current_profile())
        except CuratorError as e:
            QMessageBox.warning(self, "Please check the rules", str(e))
            return
        if not prof["sources"]:
            QMessageBox.information(self, "No sources", "Add source folders before finding duplicates.")
            self.steps.setCurrentRow(0)
            return
        self.duplicate_page.invalidate()
        self._run(W.job_duplicates, self._duplicates_ready, self.engine, prof,
                  kind=self.duplicate_page.kind.currentData(), cancellable=True)

    def _duplicates_ready(self, result):
        if result is None:
            self.status_text.setText("Duplicate search cancelled. No files were changed.")
            return
        self.duplicate_page.set_store(result["store"], result["stats"])
        self.status_text.setText("Duplicate preview ready. No files were changed.")

    def quarantine_duplicates(self):
        page = self.duplicate_page
        ids = page.selected_ids()
        if not page.store or not ids or self.thread is not None:
            return
        msg = (f"Move {len(ids):,} selected extra copies into quarantine?\n\n"
               "Retained copies are verified before each move. You can undo this run in Results.\n\n"
               f"Quarantine: {page.store.header('profile')['dedupe']['quarantine_dir']}")
        if QMessageBox.question(self, "Quarantine duplicates?", msg,
                                QMessageBox.Apply | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Apply:
            return
        self._run(W.job_apply, self._applied, self.engine, page.store, approve_deletes=True, item_ids=ids,
                  cancellable=True, pausable=True, on_fail=self._apply_failed)

    def scan_music_library(self):
        try:
            prof = P.normalize(self.current_profile())
        except CuratorError as e:
            QMessageBox.warning(self, "Please check the rules", str(e))
            return
        if not prof["sources"]:
            QMessageBox.information(self, "No sources", "Add a source folder before scanning music.")
            self.steps.setCurrentRow(0)
            return
        self._rules_changed()
        self.steps.setCurrentRow(4)
        self._run(W.job_music_library, self._music_loaded, self.engine, prof, scan=True, cancellable=True)

    def _music_loaded(self, rows):
        if rows is None:
            self.status_text.setText("Music scan cancelled. Scan again to refresh the library.")
            return
        self.music_page.set_rows(rows)
        self.status_text.setText("Music library ready. No audio files were changed.")

    def save_music_details(self, changes):
        if self.thread is not None:
            return
        try:
            count = save_details(self.engine.catalog, changes)
        except CuratorError as e:
            QMessageBox.warning(self, "Cannot save music details", str(e))
            return
        self._rules_changed()
        self.status_text.setText(f"Saved {count:,} music details. Preview organization before applying.")
        self._run(W.job_music_library, self.music_page.set_rows, self.engine, self.current_profile(), quiet=True)

    def use_music_layout(self, key, inside, keep_names):
        if self.thread is not None:
            return
        cfg = P.apply_preset(self.current_profile()["categories"]["music"], "music", key)
        cfg.pop("source_subfolder", None)
        cfg.update(enabled=True, mode="move" if keep_names else "move_and_rename")
        if inside:
            cfg["root"] = ""
        elif not cfg.get("root"):
            folder = QFileDialog.getExistingDirectory(self, "Choose music destination")
            if not folder:
                return
            cfg["root"] = folder
        with QSignalBlocker(self.dest_edits["music"]):
            self.dest_edits["music"].setText(cfg["root"])
        with QSignalBlocker(self.dest_in_source["music"]):
            self.dest_in_source["music"].setChecked(False)
        self.dest_edits["music"].setReadOnly(False)
        self.editors["music"].load(cfg)
        self.music_page.set_layout(cfg)
        self._rules_changed()
        self.status_text.setText("Music layout selected. Scan and preview to review the proposed folders.")

    def scan_and_preview(self):
        self.scan_notice.hide()
        self.scan_details_btn.hide()
        try:
            prof = P.normalize(self.current_profile())
        except CuratorError as e:
            QMessageBox.warning(self, "Please check the rules", str(e))
            return
        if not prof["sources"]:
            QMessageBox.information(self, "No sources", "Add at least one folder to organize.")
            self.steps.setCurrentRow(0)
            return
        self.profile = prof
        self.steps.setCurrentRow(2)
        self._run(W.job_scan_plan, self._plan_ready, self.engine, prof, cancellable=True)

    def replan(self):
        try:
            prof = P.normalize(self.current_profile())
        except CuratorError as e:
            QMessageBox.warning(self, "Please check the rules", str(e))
            return
        self.profile = prof
        self._run(W.job_plan, self._plan_ready, self.engine, prof, cancellable=True)

    def _plan_ready(self, res):
        if res.get("cancelled"):
            self.status_text.setText("Scan cancelled. Nothing was changed.")
            return
        self.store = res["store"]
        if res.get("music") is not None:
            self.music_page.set_rows(res["music"])
        self.model.set_store(self.store)
        self.tree.setModel(build_tree(self.store))
        self.tree.expandToDepth(1)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        s = self.store.summary()
        self.summary.setText(
            f"{s['changes']:,} to change · {s['unchanged']:,} already organized · {s['review']:,} to review · "
            f"{s['conflicts']:,} conflicts · {s['duplicates']:,} duplicates · {s['folders_to_create']:,} new folders · "
            f"{fmt_bytes(s['bytes_to_copy'])} to copy between drives")
        for k, t in self.tiles.items():
            if k == "_folders":
                t.set_value(f"{s['folders_to_create']:,}")
            elif k == "_bytes":
                t.set_value(fmt_bytes(s["bytes_to_copy"]))
            else:
                t.set_value(f"{s[k]:,}")
        self.apply_btn.setEnabled(s["changes"] > 0)
        st = res.get("stats")
        if st:
            self._protected = self._protected_folders(st)
            self.status_text.setText(f"Scanned {st.files:,} files ({st.extracted:,} read, {st.reused:,} from catalog). "
                                     f"{len(st.errors):,} scan errors · {len(self._protected):,} protected folders. Nothing has been changed.")
            self._other_details = [*st.errors, *(f"{p}: mounted folder skipped" for p in st.skipped_mounts)]
            details = [*self._other_details, *(f"{p}: {r}" for p, r in self._protected)]
            if st.skipped_symlinks:
                self._other_details.append(f"{st.skipped_symlinks:,} links skipped; linked targets may be outside the source.")
            if st.errors and sys.platform == "darwin":
                self._other_details.append("If a folder could not be read, allow Data Curator access in System Settings → Privacy & Security → Files and Folders, then scan again.")
            details = [*self._other_details, *(f"{p}: {r}" for p, r in self._protected)]
            self._scan_details = "\n".join(details)
            self.status_text.setToolTip("\n".join(details[:100]))
            self.scan_details_btn.setVisible(bool(details))
            self.scan_notice.setVisible(bool(details) or st.files == 0)
            if st.errors:
                self.scan_notice.setText(f"Scan incomplete: {len(st.errors):,} error(s). Some files could not be read. Open Scan details for the affected folders.")
            elif self._protected:
                self.scan_notice.setText(
                    f"{len(self._protected):,} protected folder(s) stay exactly as they are — apps, projects, libraries "
                    "or games whose files depend on their location. Open Scan details to review them or organize one anyway.")
            elif details:
                self.scan_notice.setText("Some folders or links were excluded. Open Scan details to see what was left out.")
            else:
                self.scan_notice.setText("No files found in the selected sources. Check the folder paths and exclusions, then scan again.")
        if s["changes"] == 0 and s["review"] and self.filter.currentData() != "all":
            self.filter.setCurrentIndex(self.filter.findData("review"))
        self._filter_changed()

    def _protected_folders(self, st):
        """Protected folders from the scan plus those the plan left in place, outermost first, de-duplicated."""
        found = dict(st.excluded_folders)
        if self.store is not None:
            from ..engine.exclusions import DirectoryExclusions
            prof = self.profile or {}
            ex = DirectoryExclusions(prof.get("scan"), roots=prof.get("sources", []), managed=P.managed_roots(prof))
            seen = set()
            for row in self.store.rows("SELECT DISTINCT source FROM items WHERE flags LIKE '%excluded%' AND action='skip'"):
                d = os.path.dirname(row[0])
                if d in seen:
                    continue
                seen.add(d)
                if hit := ex.protected_root(d):
                    found.setdefault(hit[0], hit[1])
        out = sorted(found.items())
        return [(p, r) for p, r in out if not any(p != q and p.startswith(q.rstrip(os.sep) + os.sep) for q, _ in out)]

    def show_scan_details(self):
        dlg = ScanDetailsDialog(self, getattr(self, "_protected", []), getattr(self, "_other_details", []))
        if dlg.exec() and dlg.chosen and self.exclusions.allow(dlg.chosen):
            self.status_text.setText(f"{len(dlg.chosen):,} folder(s) will be organized anyway. Scanning again…")
            self.scan_and_preview()

    def _filter_changed(self):
        self.model.set_filter(self.filter.currentData(), self.search.text().strip())
        self.details.setPlainText("")
        for k, t in getattr(self, "tiles", {}).items():
            t.set_active(k == self.filter.currentData())

    def _tile_clicked(self, key: str):
        if key.startswith("_"):
            return
        i = self.filter.findData(key)
        if i >= 0:
            self.filter.setCurrentIndex(i)

    def _selected_items(self):
        if not self.store:
            return []
        rows = sorted({i.row() for i in self.table.selectionModel().selectedRows()})
        out = []
        for r in rows:
            row = self.model.row(r)
            if row:
                it = self.store.get(row["id"])
                if it:
                    out.append(it)
        return out

    def _selection_changed(self, *_):
        items = self._selected_items()
        if len(items) == 1:
            it = items[0]
            text = f"{it.source}\n" + (f"→ {it.dest}\n" if it.dest and it.dest != it.source else "") + "\n" + explain(it)
            cands = (it.suggestions or {}).get("_candidates")
            if cands:
                text += "\n\nCandidates:\n  " + "\n  ".join(cands)
            metadata = it.payload.get("metadata")
            if metadata:
                text += "\n\nGathered metadata:\n" + json.dumps({k: v for k, v in metadata.items()
                    if not k.startswith("_")}, ensure_ascii=False, indent=2)
            self.details.setPlainText(text)
        elif items:
            self.details.setPlainText(f"{len(items)} files selected.")

    # ================================================================== review actions
    def exclude_selected(self):
        for it in self._selected_items():
            self.engine.catalog.set_excluded(it.source, True)
        self.replan()

    def include_selected(self):
        for it in self._selected_items():
            self.engine.catalog.set_excluded(it.source, False)
            self.engine.catalog.set_override(it.source, "category", None)
        self.replan()

    def accept_selected(self):
        n = 0
        for it in self._selected_items():
            for k, v in (it.suggestions or {}).items():
                if not k.startswith("_") and " or " not in str(v):
                    self.engine.catalog.set_override(it.source, k, str(v))
                    n += 1
        if not n:
            QMessageBox.information(self, "Accept suggestion",
                                    "The selected files have no single suggested value. Use “Set value…”.")
            return
        self.replan()

    def choose_match(self):
        items = self._selected_items()
        if len(items) != 1 or not (items[0].suggestions or {}).get("_candidates"):
            QMessageBox.information(self, "Choose match", "Select one movie or TV file with ambiguous matches in Review.")
            return
        item = items[0]
        candidates = item.suggestions["_candidates"]
        choice, ok = QInputDialog.getItem(self, "Choose metadata match", "Movie or TV series:", candidates, 0, False)
        if not ok:
            return
        ref = choice.rsplit("[", 1)[-1].rstrip("]")
        self.engine.catalog.set_override(item.source, "movie_id" if item.category == "movies" else "series_id", ref)
        self.replan()

    def override_selected(self):
        items = self._selected_items()
        if not items:
            return
        dlg = OverrideDialog(self, items[0].category, len(items), groups=len(items) == 1)
        if dlg.exec() != QDialog.Accepted:
            return
        field, value = dlg.field.currentText().strip(), dlg.value.text()
        for it in items:
            target = f"group:{it.group}" if dlg.scope.isChecked() and it.group else it.source
            self.engine.catalog.set_override(target, field, value if value != "" else None)
        self.replan()

    # ================================================================== apply / undo
    def apply(self):
        if not self.store:
            return
        s = self.store.summary()
        lim = self.store.count("limitations")
        msg = (f"{s['changes']:,} file operations will run.\n\n"
               f"• {s['folders_to_create']:,} folders will be created\n"
               f"• {fmt_bytes(s['bytes_to_copy'])} will be copied between drives, verified, then the originals removed\n"
               f"• {s['review']:,} files stay in place for review\n"
               f"• {s.get('protected', 0):,} protected files (projects, libraries, apps, file sets) are not touched\n"
               f"• Existing files are never overwritten\n")
        if lim:
            msg += f"• {lim:,} transfers cannot keep every attribute (see “Preservation notes”)\n"
        msg += "\nEvery step is journaled and can be undone."
        if QMessageBox.question(self, "Apply changes?", msg, QMessageBox.Apply | QMessageBox.Cancel,
                                QMessageBox.Cancel) != QMessageBox.Apply:
            return
        self.apply_btn.setEnabled(False)
        self._run(W.job_apply, self._applied, self.engine, self.store, cancellable=True, pausable=True,
                  on_fail=self._apply_failed)

    def _apply_failed(self, msg):
        if "behaves differently" in msg or "Preview again" in msg:
            if QMessageBox.question(self, "Preview again", msg + "\n\nPreview again now?") == QMessageBox.Yes:
                self.replan()
            return
        QMessageBox.warning(self, "Cannot apply", msg)

    def _applied(self, res):
        self.last_batch = res.batch_id
        self.run_tiles["done"].set_value(f"{res.done:,}")
        self.run_tiles["failed"].set_value(f"{res.failed:,}")
        self.run_tiles["skipped"].set_value(f"{res.skipped:,}")
        self.run_tiles["bytes"].set_value(fmt_bytes(res.bytes_copied))
        lines = [f"<b>{res.status.replace('_', ' ').capitalize()}</b> — {res.message}",
                 (f"{fmt_bytes(res.bytes_copied)} copied between drives and verified." if res.bytes_copied
                  else "All moves were renames on the same drive; no data was copied.")]
        self.status_text.setText(f"Run {res.batch_id}: {res.status.replace('_', ' ')}.")
        for src, why in (res.preflight + res.errors)[:200]:
            lines.append(f"• {src}: {why}")
        if res.status == "interrupted":
            lines.append("Reconnect the storage, then choose “Recover interrupted run…” to resume.")
        self.result_text.setHtml("<br>".join(lines))
        self.refresh_history()
        self.steps.setCurrentRow(3)
        self.store = None
        self.model.set_store(None)
        self.summary.setText("Applied. Run “Scan and preview” to check the result.")
        self.music_page.set_rows([])
        self.music_page.summary.setText("Files were organized. Scan music library to refresh locations.")
        self.duplicate_page.invalidate()

    def refresh_history(self):
        bs = self.engine.journal.batches(200)
        self.history.setRowCount(len(bs))
        for r, b in enumerate(bs):
            vals = [b["id"], time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created"])), b["kind"],
                    b["status"].replace("_", " "), (b.get("message") or "").split("\n")[0]]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(v)
                if c == 3 and T.is_custom():
                    col = {"completed": T.SUCCESS, "undone": T.DUPLICATE, "cancelled": T.WARNING, "interrupted": T.WARNING,
                           "failed": T.ERROR}.get(b["status"], T.QUARANTINE if "error" in b["status"] or "partial" in
                                                b["status"] else T.TEXT)
                    from PySide6.QtGui import QColor
                    item.setForeground(QColor(T.color(col)))
                self.history.setItem(r, c, item)

    def undo_selected(self):
        r = self.history.currentRow()
        if r < 0:
            QMessageBox.information(self, "Undo", "Select a run first.")
            return
        bid = self.history.item(r, 0).text()
        kind = self.history.item(r, 2).text()
        if kind != "apply":
            QMessageBox.information(self, "Undo", "Only organizing runs can be undone.")
            return
        try:
            pre = self.engine.undo(bid, dry_run=True)
        except ValueError as e:
            QMessageBox.warning(self, "Undo", str(e))
            return
        text = f"{pre.restored} item(s) can be moved back."
        if pre.conflicts:
            text += ("\n\nThese cannot be restored automatically and will be left as they are:\n• " +
                     "\n• ".join(pre.conflicts[:15]))
        if QMessageBox.question(self, "Undo this run?", text) != QMessageBox.Yes:
            return
        self._run(W.job_undo, self._undone, self.engine, bid)

    def _undone(self, rep):
        self.result_text.setPlainText(rep.summary() + ("\n" + "\n".join(rep.notes) if rep.notes else ""))
        self.refresh_history()

    def _check_interrupted(self, force=False):
        if self.thread is not None:
            return  # a run in progress is not an interrupted one
        pending = self.engine.journal.unfinished()
        if not pending:
            if force:
                QMessageBox.information(self, "Recovery", "No interrupted runs. Everything is consistent.")
            return
        box = QMessageBox(self)
        box.setWindowTitle("Interrupted run found")
        box.setText(f"{len(pending)} run(s) did not finish (for example the app closed or a drive disconnected).\n\n"
                    "Resume finishes the remaining files. Make consistent only repairs half-finished steps; "
                    "nothing is deleted either way.")
        resume = box.addButton("Resume", QMessageBox.AcceptRole)
        repair = box.addButton("Make consistent only", QMessageBox.ActionRole)
        box.addButton("Later", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is resume:
            self._run(W.job_recover, self._recovered, self.engine, resume=True)
        elif box.clickedButton() is repair:
            self._run(W.job_recover, self._recovered, self.engine, resume=False)

    def _recovered(self, rep):
        txt = "\n".join(["Recovery finished."] + [f"✓ {a}" for a in rep.actions] + [f"! {a}" for a in rep.attention])
        self.result_text.setPlainText(txt)
        self.refresh_history()
        self.steps.setCurrentRow(3)

    def export_report(self):
        if not self.store:
            QMessageBox.information(self, "Export", "Run a preview first.")
            return
        f, flt = QFileDialog.getSaveFileName(self, "Export report", f"curator-plan-{self.store.plan_id}.csv",
                                             "CSV (*.csv);;JSON (*.json)")
        if not f:
            return
        (self.store.export_json if f.endswith(".json") else self.store.export_csv)(f)
        self.statusBar().showMessage(f"Report written to {f}", 5000)

    # ================================================================== worker plumbing
    def _run(self, fn, on_done, *args, cancellable=False, pausable=False, quiet=False, on_fail=None, **kw):
        if self.thread is not None:
            QMessageBox.information(self, "Busy", "Please wait for the current task to finish.")
            return
        worker = W.Worker(fn, *args, **kw)
        self.worker = worker
        self.music_page.set_busy(True)
        self.duplicate_page.set_busy(True)
        self.pages.widget(0).setEnabled(False)
        self.pages.widget(1).setEnabled(False)
        self.pages.widget(2).setEnabled(False)
        if not quiet:
            self.progress.setVisible(True)
            self.progress.setRange(0, 0)
            self.cancel_btn.setVisible(cancellable)
            self.pause_btn.setVisible(pausable)
            self.pause_btn.setText("Pause")
            self.scan_btn.setEnabled(False)

        self._cb_done, self._cb_fail = on_done, on_fail
        # bound methods of this window: Qt queues the calls onto the UI thread
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.failed.connect(self._on_failed)
        self.thread = W.start(self, worker)

    def _on_progress(self, msg, done, total):
        self.status_text.setText(msg)
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _finish_task(self):
        self.thread = None
        self.worker = None
        self.progress.setVisible(False)
        self.cancel_btn.setVisible(False)
        self.pause_btn.setVisible(False)
        self.scan_btn.setEnabled(True)
        self.music_page.set_busy(False)
        self.duplicate_page.set_busy(False)
        self.pages.widget(0).setEnabled(True)
        self.pages.widget(1).setEnabled(True)
        self.pages.widget(2).setEnabled(True)

    def _on_finished(self, res):
        self._finish_task()
        self._cb_done(res)

    def _on_failed(self, msg):
        self._finish_task()
        self.status_text.setText("Stopped.")
        (self._cb_fail or (lambda m: QMessageBox.warning(self, "Something went wrong", m)))(msg)

    def _toggle_pause(self):
        if not self.worker:
            return
        c = self.worker.control
        if c.paused:
            c.resume()
            self.pause_btn.setText("Pause")
        else:
            c.pause()
            self.pause_btn.setText("Resume")
            self.status_text.setText("Paused after the current block. Your files are safe.")

    def _cancel(self):
        if self.worker:
            self.worker.cancel()
            self.status_text.setText("Cancelling… finishing the current file safely.")

    def closeEvent(self, ev):
        if self.thread is not None:
            if QMessageBox.question(self, "Quit?", "A task is running. Cancel it safely and quit?") != QMessageBox.Yes:
                ev.ignore()
                return
            self.worker.cancel()
            self.thread.wait(10000)
        self.engine.close()
        ev.accept()


def main(argv=None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("Data Curator")
    app.setApplicationDisplayName("Data Curator")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("DataCurator")
    app.setWindowIcon(T.app_icon())
    T.apply(app, str(QSettings("DataCurator", "DataCurator").value("theme") or "dark"))
    w = MainWindow()
    w.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
