"""GUI smoke tests (offscreen Qt). Drives the real window through the primary workflow."""
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fixtures import make_audio, make_file  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings("DataCurator", "DataCurator").clear()
    a = QApplication.instance() or QApplication([])
    yield a


def pump(app, w, timeout=30):
    end = time.time() + timeout
    while w.thread is not None and time.time() < end:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        app.processEvents()
    assert w.thread is None, "task did not finish"


def test_workflow_preview_review_apply_undo(app, engine, lib, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from datacurator.gui.app import MainWindow
    for i, a in enumerate(["A", "B", "C"], 1):
        make_audio(str(lib / f"mix/{i}.flac"), title=f"T{i}", artist=a, album="Mix", tracknumber=str(i))
    make_audio(str(lib / "lp/1.flac"), title="One", artist="X", album="LP", albumartist="X", tracknumber="1")
    make_file(str(lib / "Show.S01E01.Pilot.mkv"))
    w = MainWindow(engine)
    w.sources.addItem(str(lib))
    w.dest_edits["music"].setText(str(tmp_path / "Music"))
    w.dest_edits["tv"].setText(str(tmp_path / "TV"))
    w.scan_and_preview()
    pump(app, w)
    assert w.store is not None and w.model.rowCount() == 2  # LP track + episode
    assert "3 to review" in w.summary.text()
    assert not (tmp_path / "Music").exists()  # preview made no changes
    # template example updates immediately
    w.editors["music"].folder.setText("{album_artist}/[{year} - ]{album}")
    assert "Daft Punk/2013 - Random Access Memories" in w.editors["music"].example.text()
    w.editors["music"].folder.setText("{album_artist}/{album}")
    # review queue: accept the suggested compilation for the mixtape
    w.filter.setCurrentIndex(w.filter.findData("review"))
    app.processEvents()
    assert w.model.rowCount() == 3
    w.table.selectAll()
    w.accept_selected()
    pump(app, w)
    w.filter.setCurrentIndex(w.filter.findData("changes"))
    app.processEvents()
    assert w.model.rowCount() == 5
    # search filter
    w.search.setText("Pilot")
    w._filter_changed()
    assert w.model.rowCount() == 1
    w.search.setText("")
    w._filter_changed()
    # tree
    assert w.tree.model().rowCount() >= 1
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Apply))
    w.apply()
    pump(app, w)
    assert (tmp_path / "Music/Various Artists/Mix/01 - T1.flac").exists()
    assert (tmp_path / "TV/Show/Season 01/Show - S01E01 - Pilot.mkv").exists()
    assert w.history.rowCount() == 1
    w.history.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.undo_selected()
    pump(app, w)
    assert (lib / "mix/1.flac").exists() and (lib / "Show.S01E01.Pilot.mkv").exists()
    assert "Restored 5" in w.result_text.toPlainText()
    w.thread = None
    w.close()


def test_large_plan_table_is_paged(app, engine, lib, tmp_path):
    from datacurator.gui.app import MainWindow
    for i in range(1500):
        make_file(str(lib / f"docs/p{i % 7}/f{i:04d}.pdf"), b"%PDF" + str(i).encode())
    w = MainWindow(engine)
    w.sources.addItem(str(lib))
    w.editors["documents"].enabled.setChecked(True)
    w.dest_edits["documents"].setText(str(tmp_path / "Docs"))
    w.scan_and_preview()
    pump(app, w, 120)
    assert w.model.rowCount() == 1500
    assert w.model.row(1499) is not None
    assert len(w.model._pages) <= 2  # only touched pages were loaded
    w.close()
