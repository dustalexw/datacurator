"""Quick organize: the one-screen front end.

Drop a folder → get a suggested layout → see what will happen in plain numbers → apply → undo.
It drives the same engine, jobs and window plumbing as the step-by-step pages (kept under
"Advanced steps"), so everything stays journaled and undoable.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import profiles as P
from ..core.progress import format_bytes, format_duration
from ..models import CuratorError
from . import theme as T
from . import workers as W
from .quickmodel import explain_scan_notice, group_destinations, sample_moves, suggested_folders

LAYOUTS = (
    ("Maximum tidy — everything gets a folder (recommended)", P.organized_profile),
    ("Category folders — Music, TV, Documents, Photos, Other files", P.opening_profile),
    ("Mixed formats — folders by file type (PDF, Photos/JPEG …)", P.mixed_in_source_profile),
)
START, WORKING, REVIEW, DONE = range(4)


def _label(text="", size=None, bold=False, role=None, wrap=True) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(wrap)
    if role:
        lab.setProperty("role", role)
    style = []
    if size:
        style.append(f"font-size: {size}pt;")
    if bold:
        style.append("font-weight: 800;")
    if style:
        lab.setStyleSheet(" ".join(style))
    return lab


def _button(text: str, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setProperty("primary", True)
    return b


class DropZone(QFrame):
    """Large target that accepts dropped folders and opens a folder chooser on click."""

    chosen = Signal(list)

    def __init__(self):
        super().__init__()
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Choose folders to organize")
        self.setMinimumHeight(190)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.addWidget(_label("📂", 34, wrap=False), 0, Qt.AlignCenter)
        lay.addWidget(_label("Drop a folder here", 17, True, wrap=False), 0, Qt.AlignCenter)
        lay.addWidget(_label("or click to choose one", role="hint", wrap=False), 0, Qt.AlignCenter)
        self._style(False)

    def _style(self, hot: bool):
        accent = T.color(T.ACCENT) if T.is_custom() else "palette(highlight)"
        self.setStyleSheet(f"QFrame#DropZone {{ border: 2px dashed {accent if hot else 'palette(mid)'};"
                           f" border-radius: 20px; {'background: palette(alternate-base);' if hot else ''} }}")

    def mousePressEvent(self, ev):
        d = QFileDialog.getExistingDirectory(self, "Choose a folder to organize", os.path.expanduser("~"))
        if d:
            self.chosen.emit([d])

    def keyPressEvent(self, ev):
        if ev.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.mousePressEvent(ev)
        else:
            super().keyPressEvent(ev)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, ev):
        self._style(False)

    def dropEvent(self, ev):
        self._style(False)
        paths = []
        for u in ev.mimeData().urls():
            p = u.toLocalFile()
            if p:
                paths.append(p if os.path.isdir(p) else os.path.dirname(p))
        if paths:
            self.chosen.emit(paths)


class QuickPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.sources: list[str] = []
        self.last_batch: str | None = None
        self._t0 = 0.0
        self._phase = "Working"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(40, 34, 40, 24)
        self.views = QStackedWidget()
        lay.addWidget(self.views, 1)
        for build in (self._build_start, self._build_working, self._build_review, self._build_done):
            self.views.addWidget(build())
        self.show_view(START)

    # --------------------------------------------------------------- building
    def _build_start(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(14)
        v.addWidget(_label("Tidy a folder in one step", 24, True))
        v.addWidget(_label("Drop a messy folder. Data Curator suggests where everything belongs, shows you exactly "
                           "what will happen, and only then moves files. Everything can be undone.", role="hint"))
        self.drop = DropZone()
        self.drop.chosen.connect(self.add_sources)
        v.addWidget(self.drop)
        self.chips = QHBoxLayout()
        self.chips.setSpacing(8)
        v.addLayout(self.chips)
        v.addWidget(_label("Chosen folders", role="section", wrap=False))
        self.chosen_box = QVBoxLayout()
        self.chosen_box.setSpacing(4)
        v.addLayout(self.chosen_box)
        row = QHBoxLayout()
        row.addWidget(_label("Layout", wrap=False))
        self.layout_box = QComboBox()
        for text, _ in LAYOUTS:
            self.layout_box.addItem(text)
        self.layout_box.setAccessibleName("Layout")
        row.addWidget(self.layout_box, 1)
        v.addLayout(row)
        self.suggest_btn = _button("✦  Suggest organization", primary=True)
        self.suggest_btn.setEnabled(False)
        self.suggest_btn.clicked.connect(self.suggest)
        v.addWidget(self.suggest_btn, 0, Qt.AlignLeft)
        v.addWidget(_label("🛡  Previewing changes nothing. Protected folders (apps, projects, libraries, games) "
                           "are left alone.", role="hint"))
        v.addStretch(1)
        self._fill_chips()
        return w

    def _build_working(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(12)
        self.phase = _label("Scanning…", 22, True, wrap=False)
        self.counter = _label("", 13, wrap=False)
        self.rate = _label("", role="hint", wrap=False)
        self.bar = QProgressBar()
        self.bar.setMinimumWidth(460)
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Progress")
        self.work_cancel = _button("Cancel")
        self.work_cancel.clicked.connect(self.win._cancel)
        for x in (self.phase, self.counter, self.bar, self.rate):
            v.addWidget(x, 0, Qt.AlignCenter)
        v.addSpacing(10)
        v.addWidget(self.work_cancel, 0, Qt.AlignCenter)
        return w

    def _build_review(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setSpacing(10)
        self.hero = _label("", 24, True)
        self.hero_sub = _label("", role="hint")
        v.addWidget(self.hero)
        v.addWidget(self.hero_sub)
        tiles = QHBoxLayout()
        self.tiles = {}
        for key, text, accent in (("changes", "to organize", T.ACCENT), ("unchanged", "already in place", T.MUTED),
                                  ("review", "need your decision", T.WARNING), ("protected", "protected", T.INFO)):
            t = T.StatTile(key, text, accent)
            t.clicked.connect(self._tile)
            self.tiles[key] = t
            tiles.addWidget(t)
        v.addLayout(tiles)
        self.notice = _label("", role="hint")
        v.addWidget(self.notice)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Where files will go", "Files", "Size"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.setAccessibleName("Destination folders")
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for col in (1, 2):
            self.tree.header().setSectionResizeMode(col, QHeaderView.ResizeToContents)
        v.addWidget(self.tree, 1)
        self.samples = _label("", role="hint")
        self.samples.setTextFormat(Qt.RichText)
        v.addWidget(self.samples)
        row = QHBoxLayout()
        self.apply_btn = _button("Apply", primary=True)
        self.apply_btn.clicked.connect(self.apply)
        tune = _button("Fine-tune…")
        tune.setToolTip("Open the detailed Review page for per-file control")
        tune.clicked.connect(lambda: self.win.steps.setCurrentRow(2))
        back = _button("Start over")
        back.clicked.connect(lambda: self.show_view(START))
        row.addWidget(self.apply_btn)
        row.addWidget(tune)
        row.addStretch(1)
        row.addWidget(back)
        v.addLayout(row)
        return w

    def _build_done(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        v.setAlignment(Qt.AlignCenter)
        v.setSpacing(12)
        self.done_hero = _label("", 24, True)
        self.done_detail = _label("", role="hint")
        self.done_detail.setMaximumWidth(620)
        v.addWidget(self.done_hero, 0, Qt.AlignCenter)
        v.addWidget(self.done_detail, 0, Qt.AlignCenter)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        self.undo_btn = _button("↶  Undo everything")
        self.undo_btn.clicked.connect(self.undo)
        history = _button("View history")
        history.clicked.connect(lambda: self.win.steps.setCurrentRow(3))
        again = _button("Organize another folder", primary=True)
        again.clicked.connect(self.reset)
        for b in (self.undo_btn, history, again):
            row.addWidget(b)
        v.addLayout(row)
        return w

    # ----------------------------------------------------------------- start
    def _fill_chips(self):
        while self.chips.count():
            item = self.chips.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.chips.addWidget(_label("Quick picks:", role="hint", wrap=False))
        for p in suggested_folders(os.path.expanduser("~")):
            b = _button(os.path.basename(p))
            b.setToolTip(p)
            b.clicked.connect(lambda _=False, p=p: self.add_sources([p]))
            self.chips.addWidget(b)
        self.chips.addStretch(1)

    def add_sources(self, paths: list[str]):
        for p in paths:
            p = os.path.abspath(p)
            if os.path.isdir(p) and p not in self.sources:
                self.sources.append(p)
        self._refresh_sources()

    def _refresh_sources(self):
        while self.chosen_box.count():
            item = self.chosen_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for p in self.sources:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            h.addWidget(_label(p, wrap=False), 1)
            rm = _button("✕")
            rm.setToolTip("Remove")
            rm.setAccessibleName(f"Remove {p}")
            rm.clicked.connect(lambda _=False, p=p: self.remove_source(p))
            h.addWidget(rm)
            self.chosen_box.addWidget(row)
        self.suggest_btn.setEnabled(bool(self.sources) and not self.win.thread)

    def remove_source(self, p: str):
        if p in self.sources:
            self.sources.remove(p)
        self._refresh_sources()

    def reset(self):
        self.sources.clear()
        self._refresh_sources()
        self.show_view(START)

    def show_view(self, index: int):
        self.views.setCurrentIndex(index)

    def set_busy(self, busy: bool):
        self.suggest_btn.setEnabled(bool(self.sources) and not busy)
        self.apply_btn.setEnabled(self.apply_btn.isEnabled() and not busy)

    # --------------------------------------------------------------- suggest
    def suggest(self):
        if self.win.thread is not None:
            return
        try:
            _, builder = LAYOUTS[self.layout_box.currentIndex()]
            prof = P.normalize(builder(self.sources))
        except CuratorError as e:
            QMessageBox.warning(self, "Please check the folders", str(e))
            return
        self.win.profile = prof
        self.win._load_profile_into_ui(prof)    # Advanced pages show exactly what Quick chose
        self._begin("Scanning…")
        self.win._run(W.job_scan_plan, self._planned, self.win.engine, prof, cancellable=True,
                      on_fail=self._failed)
        if self.win.worker is not None:
            self.win.worker.progress.connect(self._progress)

    def _begin(self, phase: str):
        self._t0 = time.monotonic()
        self.phase.setText(phase)
        self.counter.setText("")
        self.rate.setText("")
        self.bar.setRange(0, 0)
        self.work_cancel.setEnabled(True)
        self.show_view(WORKING)

    def _progress(self, msg: str, done: int, total: int):
        self.phase.setText(msg.split("…")[0] + "…")
        elapsed = max(0.001, time.monotonic() - self._t0)
        rate = done / elapsed
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            eta = f" · about {format_duration((total - done) / rate)} left" if rate > 0 and done < total else ""
            self.counter.setText(f"{done:,} of {total:,}")
            self.rate.setText(f"{rate:,.0f} files/s · {format_duration(elapsed)} elapsed{eta}")
        else:
            self.counter.setText(f"{done:,} files")
            self.rate.setText(f"{rate:,.0f} files/s · {format_duration(elapsed)} elapsed")

    def _failed(self, msg: str):
        QMessageBox.warning(self, "Something went wrong", msg)
        self.show_view(START)

    def _planned(self, res):
        self.win._plan_ready(res)               # keeps the Advanced Review page in sync
        if res.get("cancelled"):
            self.show_view(START)
            return
        store, st = self.win.store, res.get("stats")
        s = store.summary()
        srcs = self.win.profile["sources"]
        groups = group_destinations(
            store.rows("SELECT dest_dir, COUNT(*), COALESCE(SUM(size),0) FROM items "
                       "WHERE action='move' AND dest_dir IS NOT NULL GROUP BY dest_dir"), srcs)
        n_folders = len(groups)
        if s["changes"]:
            self.hero.setText(f"{s['changes']:,} files → {n_folders:,} folder{'s' if n_folders != 1 else ''}")
            self.hero_sub.setText("Review the summary below. Nothing has moved yet.")
        else:
            self.hero.setText("Already organized ✓")
            self.hero_sub.setText("Nothing to move with this layout." +
                                  (f" {s['review']:,} file(s) need a decision." if s["review"] else ""))
        for key, tile in self.tiles.items():
            tile.set_value(f"{s.get(key, 0):,}")
        self.tree.clear()
        def row(name, files, size):
            it = QTreeWidgetItem([name, f"{files:,}", format_bytes(size)])
            for col in (1, 2):
                it.setTextAlignment(col, Qt.AlignRight | Qt.AlignVCenter)
            return it
        for g in groups:
            top = row(g.name, g.files, g.size)
            for c in g.sorted_children():
                top.addChild(row(c.name, c.files, c.size))
            self.tree.addTopLevelItem(top)
            top.setExpanded(len(groups) <= 6)
        for col in (1, 2):
            self.tree.headerItem().setTextAlignment(col, Qt.AlignRight)
        moves = sample_moves(store.rows("SELECT source, dest FROM items WHERE action='move' ORDER BY id LIMIT 8"),
                             srcs)
        from html import escape
        self.samples.setText("<b>Examples</b><br>" + "<br>".join(
            f"{escape(a)} &nbsp;→&nbsp; <b>{escape(b)}</b>" for a, b in moves) if moves else "")
        self.notice.setText(explain_scan_notice(st.files if st else 1, len(st.errors) if st else 0,
                                                len(self.win._protected) if st else 0))
        self.apply_btn.setText(f"Apply {s['changes']:,} change{'s' if s['changes'] != 1 else ''}")
        self.apply_btn.setEnabled(s["changes"] > 0)
        self.show_view(REVIEW)

    def _tile(self, key: str):
        """Tiles that hide detail jump to the detailed Review page, filtered."""
        if key in ("review", "protected", "changes"):
            self.win.steps.setCurrentRow(2)
            self.win._tile_clicked(key)

    # ----------------------------------------------------------------- apply
    def apply(self):
        store = self.win.store
        if store is None or self.win.thread is not None:
            return
        s = store.summary()
        msg = (f"Organize {s['changes']:,} files now?\n\n• Existing files are never overwritten\n"
               f"• {s['review']:,} files stay put for your decision\n• Everything can be undone")
        if s["bytes_to_copy"]:
            msg += f"\n• {format_bytes(s['bytes_to_copy'])} will be copied between drives and verified"
        if QMessageBox.question(self, "Apply changes?", msg, QMessageBox.Apply | QMessageBox.Cancel,
                                QMessageBox.Cancel) != QMessageBox.Apply:
            return
        self.apply_btn.setEnabled(False)
        self._begin("Organizing…")
        self.win._run(W.job_apply, self._applied, self.win.engine, store, cancellable=True, pausable=True,
                      on_fail=self._apply_failed)
        if self.win.worker is not None:
            self.win.worker.progress.connect(self._progress)

    def _apply_failed(self, msg: str):
        self.show_view(REVIEW)
        self.apply_btn.setEnabled(True)
        self.win._apply_failed(msg)

    def _applied(self, res):
        self.win._applied(res)
        self.win.show_quick()
        self.last_batch = res.batch_id
        ok = res.status in ("completed", "completed_with_errors")
        self.done_hero.setText("All done ✓" if res.status == "completed" else res.status.replace("_", " ").capitalize())
        detail = f"{res.done:,} organized"
        if res.failed or res.skipped:
            detail += f" · {res.failed:,} failed · {res.skipped:,} skipped"
        if res.bytes_copied:
            detail += f" · {format_bytes(res.bytes_copied)} copied and verified"
        problems = "\n".join(f"• {os.path.basename(src)}: {why}" for src, why in (res.preflight + res.errors)[:6])
        self.done_detail.setText(detail + (f"\n\n{problems}" if problems else "") +
                                 ("\n\nReconnect the storage, then use Advanced steps → Results to resume."
                                  if res.status == "interrupted" else ""))
        self.undo_btn.setEnabled(ok or res.status in ("cancelled",))
        self.show_view(DONE)

    # ------------------------------------------------------------------ undo
    def undo(self):
        if not self.last_batch or self.win.thread is not None:
            return
        if QMessageBox.question(self, "Undo everything?", "Put every file back where it was?",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self._begin("Undoing…")
        self.win._run(W.job_undo, self._undone, self.win.engine, self.last_batch,
                      on_fail=lambda m: (self.show_view(DONE), QMessageBox.warning(self, "Cannot undo", m)))

    def _undone(self, rep):
        self.win._undone(rep)
        self.done_hero.setText("Undone ↶")
        self.done_detail.setText(rep.summary())
        self.undo_btn.setEnabled(False)
        self.show_view(DONE)

