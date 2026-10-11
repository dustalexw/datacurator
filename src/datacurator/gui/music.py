"""Music-library page: browse, batch-edit, infer details and choose a layout."""
from __future__ import annotations

import os

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QTableView, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from .. import profiles
from ..engine.music import EDIT_FIELDS, FILENAME_MASKS, FilenameMask, validate_details
from ..models import CuratorError
from . import theme as T

COLUMNS = [("title", "Title"), ("artist", "Artist"), ("album_artist", "Album artist"),
           ("album", "Album"), ("track", "Track"), ("disc", "Disc"), ("year", "Year"),
           ("genre", "Genre"), ("format", "Format"), ("status", "Details"), ("path", "Location")]


class MusicTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = rows
        for row in rows:
            row["search"] = " ".join(str(row.get(k, "")) for k in EDIT_FIELDS).casefold() + " " + row["path"].casefold()
            row["status"] = row["error"] or ("Missing details" if row["missing"] else
                                             "Edited for organizing" if row["edited"] else "Tagged")
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section][1]

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and role in (Qt.DisplayRole, Qt.ToolTipRole, Qt.UserRole):
            value = self.rows[index.row()].get(COLUMNS[index.column()][0], "")
            if COLUMNS[index.column()][0] == "title" and value == "":
                value = os.path.basename(self.rows[index.row()]["path"])
            return value if role == Qt.UserRole else str(value) if value != "" else "—"


class MusicFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.text, self.artist, self.album, self.issue = "", "", "", "all"
        self.setSortRole(Qt.UserRole)

    def filterAcceptsRow(self, row, parent):
        r = self.sourceModel().rows[row]
        artist = r["album_artist"] or r["artist"] or "Unknown artist"
        album = r["album"] or "Unknown album"
        return (not self.text or self.text in r["search"]) and (not self.artist or self.artist == artist) and \
            (not self.album or self.album == album) and (self.issue == "all" or
                self.issue == "missing" and r["missing"] or self.issue == "errors" and bool(r["error"]) or
                self.issue == "edited" and r["edited"])


class MusicDetailsDialog(QDialog):
    def __init__(self, parent, rows):
        super().__init__(parent)
        self.setWindowTitle("Edit music details")
        layout = QVBoxLayout(self)
        text = QLabel(f"{len(rows):,} tracks selected. Check the fields to change for every selected track.\n"
                      "Changes are saved for organizing; preview again before applying to files.")
        text.setWordWrap(True)
        layout.addWidget(text)
        form = QFormLayout()
        self.fields = {}
        for field, title in EDIT_FIELDS.items():
            check = QCheckBox(title)
            edit = QLineEdit()
            values = {str(r.get(field, "")) for r in rows}
            edit.setText(next(iter(values)) if len(values) == 1 else "")
            edit.setPlaceholderText("Different values — leave unchecked to keep them")
            edit.setAccessibleName(title)
            edit.textEdited.connect(lambda _, c=check: c.setChecked(True))
            self.fields[field] = check, edit
            form.addRow(check, edit)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.values = {}

    def accept(self):
        try:
            self.values = validate_details({field: edit.text() for field, (check, edit) in self.fields.items()
                                           if check.isChecked()})
            if not self.values:
                raise CuratorError("Check at least one field to change.")
        except CuratorError as e:
            QMessageBox.warning(self, "Check music details", str(e))
            return
        super().accept()


class FilenameDetailsDialog(QDialog):
    def __init__(self, parent, rows):
        super().__init__(parent)
        self.rows = rows
        self.changes = {}
        self.setWindowTitle("Get music details from filenames")
        self.resize(920, 520)
        layout = QVBoxLayout(self)
        hint = QLabel("Choose a filename pattern, without the extension. A pattern with / starts at the source "
                      "folder. Only matching files produce suggestions.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.mask = QComboBox()
        self.mask.setEditable(True)
        self.mask.addItems(FILENAME_MASKS)
        self.settings = QSettings("DataCurator", "DataCurator")
        self.mask.setCurrentText(str(self.settings.value("music_filename_mask") or FILENAME_MASKS[0]))
        self.mask.setAccessibleName("Filename pattern")
        layout.addWidget(self.mask)
        self.missing_only = QCheckBox("Fill missing details only")
        self.missing_only.setChecked(True)
        layout.addWidget(self.missing_only)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["File", "Field", "Current", "Suggested"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Accept suggestions")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.preview)
        self.mask.currentTextChanged.connect(lambda *_: self.timer.start())
        self.missing_only.toggled.connect(self.preview)
        self.preview()

    def preview(self, *_):
        self.changes = {}
        self.table.setRowCount(0)
        try:
            parser = FilenameMask(self.mask.currentText())
            unmatched = 0
            for row in self.rows:
                try:
                    values = parser.suggest(row, self.missing_only.isChecked())
                except CuratorError:
                    unmatched += 1
                    continue
                if not values:
                    unmatched += 1
                    continue
                self.changes[row["path"]] = values
                for field, value in values.items():
                    i = self.table.rowCount()
                    self.table.insertRow(i)
                    for col, text in enumerate((row["path"], EDIT_FIELDS[field], row.get(field) or "—", value)):
                        self.table.setItem(i, col, QTableWidgetItem(str(text)))
            self.summary.setText(f"{len(self.changes):,} tracks with changes · {unmatched:,} unchanged or unmatched")
        except CuratorError as e:
            self.summary.setText(str(e))
        self.save_button.setEnabled(bool(self.changes))

    def accept(self):
        self.timer.stop()
        self.preview()  # apply the visible pattern even if the typing timer is still pending
        if self.changes:
            self.settings.setValue("music_filename_mask", self.mask.currentText())
            super().accept()


class MusicPage(QWidget):
    def __init__(self, parent, on_scan, on_edits, on_layout, on_preview):
        super().__init__(parent)
        self.on_edits = on_edits
        self.on_layout = on_layout
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 22, 28, 20)
        layout.addWidget(T.BrandTitle("Music library", 20))
        hint = QLabel("Browse your source folders by artist and album. Edit music details, choose a layout, "
                      "then preview the changes before organizing.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.summary = QLabel("Add a source folder, then scan your music library.")
        layout.addWidget(self.summary)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles, artists, albums and locations")
        self.search.setAccessibleName("Search music library")
        self.search.setClearButtonEnabled(True)
        bar.addWidget(self.search, 1)
        self.artist, self.album, self.issue = QComboBox(), QComboBox(), QComboBox()
        self.artist.setAccessibleName("Browse by artist")
        self.album.setAccessibleName("Browse by album")
        self.issue.setAccessibleName("Music details filter")
        self.artist.addItem("All artists", "")
        self.album.addItem("All albums", "")
        for text, key in (("All tracks", "all"), ("Missing details", "missing"),
                          ("Unreadable tags", "errors"), ("Edited details", "edited")):
            self.issue.addItem(text, key)
        for widget in (self.artist, self.album, self.issue):
            bar.addWidget(widget)
        layout.addLayout(bar)
        self.model = MusicTableModel(self)
        self.proxy = MusicFilter(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setAccessibleName("Music tracks")
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(-1, Qt.AscendingOrder)  # start in artist/album/disc/track order
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        widths = (180, 140, 150, 180, 50, 50, 60, 100, 70, 130, 300)
        for i, width in enumerate(widths):
            self.table.setColumnWidth(i, width)
        layout.addWidget(self.table, 1)
        actions = QHBoxLayout()
        self.scan = QPushButton("Scan music library")
        self.edit = QPushButton("Edit details…")
        self.infer = QPushButton("Details from filenames…")
        self.select_all = QPushButton("Select all shown")
        for button, callback in ((self.scan, on_scan), (self.edit, self.edit_selected),
                                 (self.infer, self.infer_selected), (self.select_all, self.table.selectAll)):
            button.clicked.connect(callback)
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.write_tags = QCheckBox("Write edited details into audio files when applying (undoable)")
        layout.addWidget(self.write_tags)
        options = QHBoxLayout()
        self.layout_preset = QComboBox()
        self.layout_preset.setAccessibleName("Music organization layout")
        for key, preset in profiles.PRESETS["music"].items():
            self.layout_preset.addItem(preset["label"], key)
        self.inside_source = QCheckBox("Organize inside each source folder")
        self.inside_source.setChecked(True)
        self.keep_names = QCheckBox("Keep filenames")
        self.use_layout = QPushButton("Use layout")
        self.use_layout.clicked.connect(lambda: on_layout(self.layout_preset.currentData(),
                                                         self.inside_source.isChecked(), self.keep_names.isChecked()))
        for widget in (self.layout_preset, self.inside_source, self.keep_names, self.use_layout):
            options.addWidget(widget)
        layout.addLayout(options)
        self.preview_button = QPushButton("Scan and preview organization")
        self.preview_button.clicked.connect(on_preview)
        layout.addWidget(self.preview_button)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(200)
        self.search_timer.timeout.connect(self.filter_changed)
        self.search.textChanged.connect(lambda *_: self.search_timer.start())
        self.artist.currentIndexChanged.connect(self.artist_changed)
        self.album.currentIndexChanged.connect(self.filter_changed)
        self.issue.currentIndexChanged.connect(self.filter_changed)
        self.table.selectionModel().selectionChanged.connect(self.selection_changed)
        self.selection_changed()

    def set_rows(self, rows):
        selected_artist = self.artist.currentData()
        self.model.set_rows(rows)
        self.artist.blockSignals(True)
        self.artist.clear()
        self.artist.addItem("All artists", "")
        for artist in sorted({r["album_artist"] or r["artist"] or "Unknown artist" for r in rows}, key=str.casefold):
            self.artist.addItem(artist, artist)
        self.artist.setCurrentIndex(max(0, self.artist.findData(selected_artist)))
        self.artist.blockSignals(False)
        self.artist_changed()

    def artist_changed(self, *_):
        selected_album = self.album.currentData()
        artist = self.artist.currentData()
        self.album.blockSignals(True)
        self.album.clear()
        self.album.addItem("All albums", "")
        albums = {r["album"] or "Unknown album" for r in self.model.rows
                  if not artist or (r["album_artist"] or r["artist"] or "Unknown artist") == artist}
        for album in sorted(albums, key=str.casefold):
            self.album.addItem(album, album)
        self.album.setCurrentIndex(max(0, self.album.findData(selected_album)))
        self.album.blockSignals(False)
        self.filter_changed()

    def filter_changed(self, *_):
        self.proxy.text = self.search.text().strip().casefold()
        self.proxy.artist, self.proxy.album = self.artist.currentData(), self.album.currentData()
        self.proxy.issue = self.issue.currentData()
        self.proxy.invalidate()
        self.summary.setText(f"{self.proxy.rowCount():,} shown · {len(self.model.rows):,} tracks in scanned sources · "
                             f"{sum(r['missing'] for r in self.model.rows):,} missing details")
        self.selection_changed()

    def selected_rows(self):
        return [self.model.rows[self.proxy.mapToSource(index).row()]
                for index in self.table.selectionModel().selectedRows()]

    def selection_changed(self, *_):
        selected = bool(self.table.selectionModel().selectedRows())
        self.edit.setEnabled(selected and self.scan.isEnabled())
        self.infer.setEnabled(selected and self.scan.isEnabled())

    def set_busy(self, busy):
        for widget in (self.scan, self.use_layout, self.preview_button, self.write_tags):
            widget.setEnabled(not busy)
        self.selection_changed()

    def edit_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        dialog = MusicDetailsDialog(self, rows)
        if dialog.exec() == QDialog.Accepted:
            self.on_edits({row["path"]: dialog.values for row in rows})

    def infer_selected(self):
        rows = self.selected_rows()
        if not rows:
            return
        dialog = FilenameDetailsDialog(self, rows)
        if dialog.exec() == QDialog.Accepted:
            self.on_edits(dialog.changes)

    def set_layout(self, cfg):
        self.inside_source.setChecked(not cfg.get("root"))
        self.keep_names.setChecked(cfg.get("mode") == "move")
        self.layout_preset.setCurrentIndex(max(0, self.layout_preset.findData(cfg.get("preset"))))
