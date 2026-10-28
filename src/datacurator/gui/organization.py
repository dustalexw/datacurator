"""Folder rules, scan protections, video lookup settings, and exact-duplicate review."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QSignalBlocker
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QListWidget, QMessageBox,
    QPlainTextEdit, QPushButton, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ..engine.executor import fmt_bytes
from ..models import CuratorError
from .models import PlanTableModel
from . import theme as T


def hint(text):
    w = QLabel(text)
    w.setWordWrap(True)
    w.setProperty("role", "hint")
    return w


class ExtensionFolders(QWidget):
    def __init__(self, changed):
        super().__init__()
        self.changed = changed
        lay = QVBoxLayout(self)
        self.enabled = QCheckBox("Sort selected extensions into their own folders")
        lay.addWidget(self.enabled)
        lay.addWidget(hint("Leave destinations empty to create these folders inside each source. "
            "For example: .pdf → PDF, .zip → Archives. Folder paths are relative to the category destination. "
            "Music and video formats prefix their normal artist/album or series layout; companions stay with their media."))
        self.table = QTableWidget(0, 2)
        self.table.setAccessibleName("Extension folder rules")
        self.table.setHorizontalHeaderLabels(["Extensions (comma separated)", "Folder"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        lay.addWidget(self.table, 1)
        bar = QHBoxLayout()
        for text, fn in (("Add rule", self.add_rule), ("Remove selected", self.remove_rules)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)
        self.enabled.toggled.connect(changed)
        self.table.itemChanged.connect(changed)

    def add_rule(self):
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(".txt"))
        self.table.setItem(row, 1, QTableWidgetItem("Text"))
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))
        self.changed()

    def remove_rules(self):
        for row in sorted({i.row() for i in self.table.selectionModel().selectedRows()}, reverse=True):
            self.table.removeRow(row)
        self.changed()

    def load(self, cfg):
        with QSignalBlocker(self.table), QSignalBlocker(self.enabled):
            self.enabled.setChecked(bool(cfg.get("enabled")))
            grouped = {}
            for ext, folder in cfg.get("folders", {}).items():
                grouped.setdefault(folder, []).append(ext)
            self.table.setRowCount(len(grouped))
            for row, (folder, extensions) in enumerate(grouped.items()):
                self.table.setItem(row, 0, QTableWidgetItem(", ".join(extensions)))
                self.table.setItem(row, 1, QTableWidgetItem(folder))

    def dump(self):
        folders = {}
        for row in range(self.table.rowCount()):
            ext = self.table.item(row, 0)
            folder = self.table.item(row, 1)
            extensions, dest = (ext.text().strip() if ext else ""), (folder.text().strip() if folder else "")
            if not extensions and not dest:
                continue
            if not extensions or not dest:
                raise CuratorError("Each extension rule needs extensions and a folder.")
            for key in extensions.split(","):
                key = "." + key.strip().lower().lstrip(".")
                if key in folders:
                    raise CuratorError(f"Extension {key} has more than one folder rule.")
                folders[key] = dest
        return {"enabled": self.enabled.isChecked(), "folders": folders}


class _FolderList(QWidget):
    """A titled, multi-select list of folders with Add/Remove buttons."""

    def __init__(self, title, accessible, dialog_title, changed):
        super().__init__()
        self.changed, self.dialog_title = changed, dialog_title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel(title))
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setAccessibleName(accessible)
        lay.addWidget(self.list, 1)
        bar = QHBoxLayout()
        for text, fn in (("Add folder…", self.add_folder), ("Remove selected", self.remove_folders)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)

    def add(self, folder):
        if folder and not self.list.findItems(folder, Qt.MatchExactly):
            self.list.addItem(folder)
            return True
        return False

    def add_folder(self):
        if self.add(QFileDialog.getExistingDirectory(self, self.dialog_title)):
            self.changed()

    def remove_folders(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))
        self.changed()

    def remove(self, folder):
        for item in self.list.findItems(folder, Qt.MatchExactly):
            self.list.takeItem(self.list.row(item))

    def set(self, folders):
        self.list.clear()
        self.list.addItems(folders)

    def values(self):
        return [self.list.item(i).text() for i in range(self.list.count())]

    def addItem(self, folder):  # QListWidget-style convenience
        self.add(folder)

    def count(self):
        return self.list.count()


class Exclusions(QWidget):
    def __init__(self, changed):
        super().__init__()
        self.changed = changed
        lay = QVBoxLayout(self)
        self.games = QCheckBox("Protect folders whose files depend on staying where they are (recommended)")
        self.games.setAccessibleName("Protect dependent folders")
        lay.addWidget(self.games)
        lay.addWidget(hint(
            "Data Curator never moves files out of: apps and bundles (.app, Photos and Music libraries, "
            "Final Cut and Logic projects), software projects (Git, package.json, Xcode …), creative projects "
            "(Ableton, Premiere, Pro Tools, InDesign …), installed games, virtual machines, DVD/Blu-ray folders, "
            "device backups, saved web pages and hidden settings folders. A source folder itself is only "
            "protected by unmistakable signs such as a Git repository, so a stray file never freezes your "
            "Downloads. Protected folders are listed in Scan details after each scan."))
        self.folders = _FolderList("Folders to leave alone", "Excluded folders",
                                   "Leave this folder and its contents alone", changed)
        lay.addWidget(self.folders, 1)
        self.allowed = _FolderList("Organize anyway (lifts automatic protection; app bundles and repository data "
                                   "inside stay protected)", "Folders to organize anyway",
                                   "Organize this protected folder anyway", changed)
        lay.addWidget(self.allowed, 1)
        self.games.toggled.connect(changed)

    # compatibility with earlier callers
    def add_folder(self):
        self.folders.add_folder()

    def remove_folders(self):
        self.folders.remove_folders()

    def allow(self, folders):
        """Organize these protected folders anyway; returns True if anything changed."""
        added = [f for f in folders if self.allowed.add(f)]
        for f in added:
            self.folders.remove(f)
        if added:
            self.changed()
        return bool(added)

    def load(self, cfg):
        with QSignalBlocker(self.games):
            self.games.setChecked(cfg.get("exclude_games", True))
        self.folders.set(cfg.get("exclude_dirs", []))
        self.allowed.set(cfg.get("allow_dirs", []))

    def dump(self):
        return {"exclude_games": self.games.isChecked(), "exclude_dirs": self.folders.values(),
                "allow_dirs": self.allowed.values()}


class ScanDetailsDialog(QDialog):
    """Protected folders (with “Organize anyway”), plus folders and links that could not be scanned."""

    def __init__(self, parent, protected, other_lines):
        super().__init__(parent)
        self.setWindowTitle("Scan details")
        self.resize(820, 520)
        self.chosen: list[str] = []
        lay = QVBoxLayout(self)
        lay.addWidget(T.BrandTitle("Protected folders", 17))
        lay.addWidget(hint(
            "These folders stay exactly as they are, because the files inside depend on their location. "
            "If one was protected by mistake, select it and choose Organize anyway."))
        self.table = QTableWidget(len(protected), 2)
        self.table.setAccessibleName("Protected folders")
        self.table.setHorizontalHeaderLabels(["Folder", "Why it is protected"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for row, (path, why) in enumerate(protected):
            item = QTableWidgetItem(path)
            item.setToolTip(path)
            self.table.setItem(row, 0, item)
            self.table.setItem(row, 1, QTableWidgetItem(why))
        lay.addWidget(self.table, 2)
        bar = QHBoxLayout()
        self.reveal_btn = QPushButton("Show in file browser")
        self.reveal_btn.clicked.connect(self.reveal)
        self.allow_btn = QPushButton("Organize selected anyway…")
        self.allow_btn.clicked.connect(self.allow)
        explicit = [p for p, why in protected if why == "Excluded folder"]
        self.allow_btn.setToolTip("Lift the automatic protection, then scan again. Folders you excluded yourself "
                                  "are managed under Rules → Exclusions.")
        for b in (self.reveal_btn, self.allow_btn):
            b.setEnabled(bool(protected))
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)
        self._explicit = set(explicit)
        if other_lines:
            lay.addWidget(QLabel("Not scanned"))
            text = QPlainTextEdit("\n".join(other_lines))
            text.setReadOnly(True)
            text.setAccessibleName("Folders and links not scanned")
            lay.addWidget(text, 1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        lay.addWidget(close)

    def _selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, 0).text() for r in rows]

    def reveal(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        for path in self._selected()[:5]:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def allow(self):
        chosen = [p for p in self._selected() if p not in self._explicit]
        if not chosen:
            QMessageBox.information(self, "Organize anyway", "Select a protected folder first. Folders you "
                                    "excluded yourself are changed under Rules → Exclusions.")
            return
        if QMessageBox.question(
                self, "Organize anyway?",
                f"Data Curator will organize files inside {len(chosen):,} protected folder(s) the next time you "
                "scan. Apps or projects that expect those files in place may stop finding them.\n\n"
                "You will still review every change before anything moves, and every run can be undone.",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.chosen = chosen
        self.accept()


class VideoMetadata(QWidget):
    def __init__(self, changed):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.addWidget(hint("Local parsing is always available: movie years, S01E02/1x02/date/absolute episode "
            "names, matching NFO files, movie.nfo, tvshow.nfo, and embedded MP4/M4V tags. "
            "Optional online lookups fill missing details. Ambiguous matches wait in Review; use Choose match… "
            "to pick a movie or series. Select a file to see gathered metadata and its source."))
        form = QFormLayout()
        self.tv = QComboBox()
        self.tv.addItem("Local parsing only", "none")
        self.tv.addItem("Local parsing + TVmaze", "tvmaze")
        self.movies = QComboBox()
        self.movies.addItem("Local parsing only", "none")
        self.movies.addItem("Local parsing + TMDB", "tmdb")
        form.addRow("TV shows", self.tv)
        form.addRow("Movies", self.movies)
        token = QPushButton("Set TMDB read token…")
        token.clicked.connect(self.set_token)
        form.addRow("Movie lookup access", token)
        lay.addLayout(form)
        links = QLabel('<a href="https://www.themoviedb.org/settings/api">Get a TMDB API Read Access Token</a> · '
                       '<a href="https://www.tvmaze.com/api">TVmaze API</a>')
        links.setOpenExternalLinks(True)
        lay.addWidget(links)
        lay.addWidget(hint("The token is stored in your system credential store and is never included in profiles. "
            "Online lookup sends parsed movie/series names to the selected service. Results are cached for 30 days."))
        lay.addStretch()
        self.tv.currentIndexChanged.connect(changed)
        self.movies.currentIndexChanged.connect(changed)

    def set_token(self):
        token, ok = QInputDialog.getText(self, "TMDB movie lookup", "API Read Access Token:", QLineEdit.Password)
        if not ok or not token.strip():
            return
        try:
            from ..metadata.providers import store_tmdb_token
            store_tmdb_token(token)
        except Exception:
            QMessageBox.warning(self, "Could not store token", "The system credential store is unavailable.")
            return
        QMessageBox.information(self, "Movie lookup", "Token saved securely. Select TMDB and preview again.")

    def load(self, cfg):
        for key, widget in (("tv", self.tv), ("movies", self.movies)):
            with QSignalBlocker(widget):
                widget.setCurrentIndex(max(0, widget.findData(cfg.get(key, "none"))))


class DuplicateModel(PlanTableModel):
    def columnCount(self, parent=None):
        return 4

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return ["Extra copy", "Copy to keep", "Quarantine location", "Size"][section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        row = self.row(index.row())
        if row is None:
            return None
        if role in (Qt.DisplayRole, Qt.AccessibleTextRole, Qt.ToolTipRole):
            item = self.store.get(row["id"])
            values = [item.source, item.payload.get("keeper"), item.dest, fmt_bytes(item.size)]
            if role != Qt.ToolTipRole and self._base:
                values[:2] = [os.path.relpath(path, self._base) for path in values[:2]]
            return values[index.column()]
        if role == Qt.UserRole:
            return row["id"]
        return None


class DuplicatePage(QWidget):
    def __init__(self, parent, find, apply, changed):
        super().__init__(parent)
        self.setObjectName("Page")
        self.store = None
        self.busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(28, 22, 28, 20)
        lay.addWidget(T.BrandTitle("Find identical copies", 20))
        lay.addWidget(hint("Search the selected sources by file size, then verify matching content. "
            "Choose extra copies to move to quarantine; the retained copy is checked again before each move. "
            "Quarantine moves can be undone in Results."))
        form = QFormLayout()
        self.kind = QComboBox()
        for text, key in (("All files", None), ("Music", "audio"), ("Videos", "video"),
                          ("Photos", "image"), ("Documents", "document")):
            self.kind.addItem(text, key)
        form.addRow("Find duplicates in", self.kind)
        self.controls = [self.kind]
        self.prefer = QLineEdit()
        self.prefer.setPlaceholderText("Optional: favor copies in this folder")
        self.quarantine = QLineEdit()
        self.quarantine.setPlaceholderText("Default: Data Curator's duplicate quarantine")
        for text, entry in (("Prefer keeping copies in", self.prefer), ("Quarantine folder", self.quarantine)):
            bar = QHBoxLayout()
            bar.addWidget(entry, 1)
            b = QPushButton("Browse…")
            b.clicked.connect(lambda _=False, e=entry: self.browse(e))
            self.controls.extend([entry, b])
            bar.addWidget(b)
            form.addRow(text, bar)
            entry.textChanged.connect(changed)
        self.kind.currentIndexChanged.connect(changed)
        lay.addLayout(form)
        self.summary = hint("Find duplicates to see which copies can be set aside.")
        lay.addWidget(self.summary)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search duplicate paths")
        lay.addWidget(self.search)
        self.table = QTableView()
        self.table.setAccessibleName("Duplicate copies and retained originals")
        self.model = DuplicateModel(self)
        self.model.filter = "all"
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setStretchLastSection(True)
        for i in range(3):
            self.table.setColumnWidth(i, 310)
        lay.addWidget(self.table, 1)
        self.search.textChanged.connect(lambda text: self.model.set_filter("all", text.strip()))
        self.table.selectionModel().selectionChanged.connect(self.update_buttons)
        bar = QHBoxLayout()
        self.find_btn = QPushButton("Find duplicates")
        self.find_btn.clicked.connect(find)
        bar.addWidget(self.find_btn)
        select = QPushButton("Select displayed copies")
        select.clicked.connect(self.table.selectAll)
        bar.addWidget(select)
        bar.addStretch()
        self.apply_btn = QPushButton("Quarantine selected copies…")
        self.apply_btn.setProperty("primary", True)
        self.apply_btn.clicked.connect(apply)
        bar.addWidget(self.apply_btn)
        lay.addLayout(bar)
        self.update_buttons()

    def browse(self, entry):
        folder = QFileDialog.getExistingDirectory(self, "Choose folder")
        if folder:
            entry.setText(folder)

    def load(self, cfg):
        for key, entry in (("quarantine_dir", self.quarantine), ("prefer_root", self.prefer)):
            with QSignalBlocker(entry):
                entry.setText(cfg.get(key, ""))
        self.invalidate()

    def dump(self):
        return {"quarantine_dir": self.quarantine.text().strip(), "prefer_root": self.prefer.text().strip()}

    def selected_ids(self):
        return {self.model.row(i.row())["id"] for i in self.table.selectionModel().selectedRows()}

    def update_buttons(self, *_):
        self.find_btn.setEnabled(not self.busy)
        self.apply_btn.setEnabled(not self.busy and self.store is not None and bool(self.selected_ids()))

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.controls:
            widget.setEnabled(not busy)
        self.update_buttons()

    def invalidate(self):
        if self.store:
            self.store.close()
        self.store = None
        self.model.set_store(None)
        self.summary.setText("Find duplicates again after changing sources or rules.")
        self.update_buttons()

    def set_store(self, store, stats):
        self.invalidate()
        self.store = store
        self.model.set_store(store)
        groups = store.header("duplicate_groups", [])
        errors = store.header("duplicate_errors", [])
        self.summary.setText(f"{len(groups):,} duplicate groups · {store.count():,} extra copies · "
            f"{fmt_bytes(store.header('duplicate_bytes', 0))} in extra copies · "
            f"{len(stats.excluded_folders):,} protected folders skipped · {len(errors):,} unreadable/changed files. "
            "Select the copies you want to quarantine.")
        self.summary.setToolTip("\n".join(errors[:100] + [f"{p}: {r}" for p, r in stats.excluded_folders[:100]]))
        self.update_buttons()
