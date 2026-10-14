"""GUI smoke tests (offscreen Qt). Drives the real window through the primary workflow."""
import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from fixtures import make_audio, make_file  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    QSettings.setDefaultFormat(QSettings.IniFormat)
    QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, str(tmp_path_factory.mktemp("gui-settings")))
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
    assert w.store is not None and w.model.rowCount() == 5  # all detected files, including review rows
    w.filter.setCurrentIndex(w.filter.findData("changes"))
    assert w.model.rowCount() == 2  # LP track + episode
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


def test_music_library_scan_filter_batch_edit_layout_and_preview(app, engine, lib, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog
    from datacurator.gui.app import MainWindow
    from datacurator.gui.music import MusicDetailsDialog, FilenameDetailsDialog
    a = lib / "Artist/Album/01 - One.flac"
    b = lib / "Artist/Album/02 - Two.flac"
    make_audio(str(a), artist="Artist", album="Album", title="One", tracknumber="1")
    make_audio(str(b))
    original = {path: path.read_bytes() for path in (a, b)}
    w = MainWindow(engine)
    w.sources.addItem(str(lib))
    w.scan_music_library()
    pump(app, w)
    page = w.music_page
    assert w.steps.currentRow() == 4 and page.proxy.rowCount() == 2
    page.issue.setCurrentIndex(page.issue.findData("missing"))
    assert page.proxy.rowCount() == 1
    page.table.selectAll()
    dialog = FilenameDetailsDialog(page, page.selected_rows())
    dialog.mask.setCurrentText("{album_artist}/{album}/{track} - {title}")
    dialog.preview()
    assert dialog.changes[str(b)] == {"album_artist": "Artist", "album": "Album", "track": "2", "title": "Two"}
    w.save_music_details(dialog.changes)
    pump(app, w)
    assert page.proxy.rowCount() == 0
    dialog.close()
    page.issue.setCurrentIndex(page.issue.findData("all"))
    page.artist.setCurrentIndex(page.artist.findData("Artist"))
    assert page.proxy.rowCount() == 2
    page.search.setText("Two")
    page.filter_changed()
    assert page.proxy.rowCount() == 1
    page.search.clear()
    page.filter_changed()
    page.table.selectAll()

    def edit_dialog(d):
        # Check only album; keep distinct titles and track numbers unchanged.
        check, entry = d.fields["album"]
        check.setChecked(True)
        entry.setText("New Album")
        d.accept()
        return QDialog.Accepted

    monkeypatch.setattr(MusicDetailsDialog, "exec", edit_dialog)
    page.edit_selected()
    pump(app, w)
    assert {row["album"] for row in page.model.rows} == {"New Album"}
    assert {row["title"] for row in page.model.rows} == {"One", "Two"}
    w.dest_edits["music"].setText(str(tmp_path / "Old destination"))
    w.use_music_layout("music_in_source", True, True)
    assert w.dest_edits["music"].text() == ""
    assert w.current_profile()["categories"]["music"]["mode"] == "move"
    page.write_tags.setChecked(True)
    assert w.opt_tags.isChecked() and w.current_profile()["write_tags"]
    page.write_tags.setChecked(False)
    w.scan_and_preview()
    pump(app, w)
    assert all(w.store.by_source(str(path)).dest == str(lib / "Music/Artist/New Album" / path.name)
               for path in (a, b))
    assert all(path.read_bytes() == data for path, data in original.items())
    assert w.apply_btn.isEnabled()
    w.save_music_details({str(a): {"genre": "Jazz"}})
    assert not w.apply_btn.isEnabled()  # stale plans cannot apply edited details
    pump(app, w)
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


def test_destinations_enable_photos_documents_and_preserve_loaded_disabled_rules(app, engine, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    from datacurator.gui.app import MainWindow
    from datacurator import profiles
    w = MainWindow(engine)
    p = profiles.normalize({"categories": {
        "documents": {"root": str(tmp_path / "Docs"), "enabled": False},
        "photos": {"root": str(tmp_path / "Photos"), "enabled": False}}})
    w._load_profile_into_ui(p)
    assert not w.editors["documents"].enabled.isChecked()
    assert not w.editors["photos"].enabled.isChecked()
    w.dest_edits["documents"].setText(str(tmp_path / "Documents"))
    w.dest_edits["photos"].setText(str(tmp_path / "Pictures"))
    assert w.current_profile()["categories"]["documents"]["enabled"]
    assert w.current_profile()["categories"]["photos"]["enabled"]
    w.editors["documents"].enabled.setChecked(False)
    assert not w.current_profile()["categories"]["documents"]["enabled"]
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a: str(tmp_path / "Documents")))
    w.browse_dest("documents")  # choosing the existing destination also enables its rule
    assert w.current_profile()["categories"]["documents"]["enabled"]
    w.close()


def test_extension_exclusion_video_controls_and_duplicate_quarantine(app, engine, lib, tmp_path, monkeypatch):
    from datacurator.gui.app import MainWindow
    from datacurator import profiles
    from PySide6.QtWidgets import QMessageBox
    for folder in ("a", "keep", "steamapps"):
        make_file(str(lib / folder / "same.pdf"), b"same PDF")
    p = profiles.normalize({"sources": [str(lib)], "dedupe": {"prefer_root": str(lib / "keep")},
                           "extension_folders": {"enabled": True, "folders": {".pdf": "PDF"}}})
    w = MainWindow(engine)
    w._load_profile_into_ui(p)
    assert w.extension_rules.enabled.isChecked()
    assert w.extension_rules.table.rowCount() == 1
    assert w.exclusions.games.isChecked()
    assert w.current_profile()["providers"] == {"tv": "none", "movies": "none"}
    w.video_metadata.movies.setCurrentIndex(w.video_metadata.movies.findData("tmdb"))
    assert w.current_profile()["providers"]["movies"] == "tmdb"
    w.video_metadata.movies.setCurrentIndex(0)
    assert w.current_profile()["extension_folders"]["folders"] == {".pdf": "PDF"}
    w.steps.setCurrentRow(5)
    w.duplicate_page.kind.setCurrentIndex(w.duplicate_page.kind.findData("document"))
    w.find_duplicates()
    pump(app, w)
    page = w.duplicate_page
    assert page.model.rowCount() == 1 and "1 protected" in page.summary.text()
    assert next(page.store.items()).payload["keeper"] == str(lib / "keep/same.pdf")
    page.table.selectAll()
    assert page.apply_btn.isEnabled()
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Apply))
    w.quarantine_duplicates()
    pump(app, w)
    assert (lib / "keep/same.pdf").exists() and not (lib / "a/same.pdf").exists()
    assert (lib / "steamapps/same.pdf").exists() and page.store is None
    w.history.selectRow(0)
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.Yes))
    w.undo_selected()
    pump(app, w)
    assert (lib / "a/same.pdf").exists()
    w.find_duplicates()
    pump(app, w)
    page.table.selectAll()
    w.exclusions.games.setChecked(False)
    assert page.store is None and not page.apply_btn.isEnabled()
    w.close()


def test_movie_lookup_review_choose_match(app, engine, lib, monkeypatch):
    from PySide6.QtWidgets import QInputDialog
    from datacurator.gui.app import MainWindow
    from datacurator.metadata.providers import Candidate
    movie = make_file(str(lib / "Dune.mkv"))
    class Provider:
        name = "tmdb"
        def search(self, name):
            return [Candidate("tmdb", "1", "Dune", 1984), Candidate("tmdb", "2", "Dune", 2021)]
        def details(self, ref):
            return {"title": "Dune", "year": 2021, "plot": "Online plot"}
    monkeypatch.setattr("datacurator.metadata.providers.make_movie_provider", lambda *a: Provider())
    w = MainWindow(engine);w.sources.addItem(str(lib));w.scan_and_preview();pump(app, w)
    assert w.model.rowCount() == 1 and w.filter.currentData() == "all"
    w.table.selectRow(0)
    assert "[tmdb:2]" in w.details.toPlainText()
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a: (a[3][1], True)))
    w.choose_match();pump(app, w)
    item = w.store.by_source(movie)
    assert item.action == "move" and item.dest.endswith("Dune (2021)/Dune (2021).mkv")
    w.filter.setCurrentIndex(w.filter.findData("changes"));w.table.selectRow(0)
    assert "Online plot" in w.details.toPlainText()
    w.close()


def test_one_click_mixed_layout_clears_destinations_preserves_exclusions(app, engine, lib, tmp_path):
    from datacurator.gui.app import MainWindow
    w = MainWindow(engine);w.sources.addItem(str(lib))
    w.dest_edits["documents"].setText(str(tmp_path / "External"))
    w.exclusions.folders.addItem(str(lib / "Saved games"))
    w.video_metadata.movies.setCurrentIndex(w.video_metadata.movies.findData("tmdb"))
    w.use_mixed_layout()
    p = w.current_profile()
    assert p["sources"] == [str(lib)] and all(not c["root"] for c in p["categories"].values())
    assert p["categories"]["photos"]["enabled"] and p["categories"]["documents"]["enabled"]
    assert p["extension_folders"]["folders"][".heic"] == "Photos/HEIC"
    assert p["scan"]["exclude_dirs"] == [str(lib / "Saved games")]
    assert p["providers"] == {"tv": "none", "movies": "none"}
    assert w.steps.currentRow() == 1 and w.cat_tabs.currentWidget() is w.extension_rules
    w.close()


def test_opening_category_destinations_follow_sources_and_preserve_custom_paths(app, engine, tmp_path):
    from PySide6.QtCore import QSettings
    from datacurator import profiles as P
    from datacurator.gui.app import MainWindow
    QSettings('DataCurator', 'DataCurator').remove('last_profile')
    w = MainWindow(engine)
    first, second = tmp_path / 'Downloads', tmp_path / 'Other source'
    first.mkdir(); second.mkdir()
    w.sources.addItem(str(first))
    assert w.opt_all_files.isChecked() and w.filter.currentData() == 'all'
    for cat, folder in P.CATEGORY_FOLDERS.items():
        assert w.dest_in_source[cat].isChecked() and w.dest_edits[cat].isReadOnly()
        assert w.dest_edits[cat].text() == str(first / folder)
        assert w.current_profile()['categories'][cat]['enabled']
    custom = str(tmp_path / 'My music')
    w.dest_edits['music'].setText(custom)
    assert not w.dest_in_source['music'].isChecked() and not w.dest_edits['music'].isReadOnly()
    w.sources.addItem(str(second))
    p = P.normalize(w.current_profile())
    assert p['categories']['music']['root'] == custom
    assert not p['categories']['music'].get('source_subfolder')
    assert w.dest_edits['documents'].text() == 'Each source / Documents'
    assert str(first / 'Documents') in w.dest_edits['documents'].toolTip()
    w.sources.takeItem(0)
    assert w.dest_edits['documents'].text() == str(second / 'Documents')
    assert w.dest_edits['music'].text() == custom
    saved = tmp_path / 'categories.json'; P.save(w.current_profile(), str(saved))
    w._load_profile_into_ui(P.load(str(saved)))
    assert w.dest_edits['documents'].text() == str(second / 'Documents')
    legacy = P.normalize({'sources': [str(first)], 'categories': {'documents': {'enabled': False, 'root': custom}}})
    w._load_profile_into_ui(legacy)
    assert not w.editors['documents'].enabled.isChecked() and w.dest_edits['documents'].text() == custom
    assert not w.dest_in_source['documents'].isChecked()
    w.use_category_layout()
    assert all(w.current_profile()['categories'][cat]['enabled'] for cat in P.CATEGORY_FOLDERS)
    assert w.dest_edits['documents'].text() == str(first / 'Documents') and w.opt_all_files.isChecked()
    w.close()


def test_all_file_preview_reports_permission_errors_and_lists_unknown_and_hidden_files(app, engine, lib, monkeypatch):
    import errno
    from PySide6.QtCore import QSettings
    from datacurator.gui.app import MainWindow
    from datacurator.engine import scanner
    QSettings('DataCurator', 'DataCurator').remove('last_profile')
    for name in ('.hidden', 'unknown', 'nested/archive.zip', 'Documents/organized.pdf', 'Blocked/a.pdf'):
        make_file(str(lib / name))
    real = scanner.os.scandir
    def denied(path):
        if str(path) == str(lib / 'Blocked'):
            raise PermissionError(errno.EACCES, 'Permission denied', path)
        return real(path)
    monkeypatch.setattr(scanner.os, 'scandir', denied)
    w = MainWindow(engine); w.sources.addItem(str(lib)); w.scan_and_preview(); pump(app, w)
    assert w.model.rowCount() == 4 and w.filter.currentData() == 'all'
    assert not w.scan_notice.isHidden() and 'Scan incomplete' in w.scan_notice.text()
    assert not w.scan_details_btn.isHidden() and 'Blocked' in w._scan_details
    assert '1 scan errors' in w.status_text.text()
    assert not (lib / 'Other files').exists()
    monkeypatch.setattr(scanner.os, 'scandir', real)
    w.scan_and_preview(); pump(app, w)
    assert w.model.rowCount() == 5 and w.scan_notice.isHidden() and w.scan_details_btn.isHidden()
    w.close()
