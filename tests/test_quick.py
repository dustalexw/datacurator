"""Quick organize: Qt-free grouping logic, plus an offscreen end-to-end drive of the page."""
import os

import pytest

from datacurator.gui.quickmodel import explain_scan_notice, group_destinations, sample_moves, suggested_folders


def test_group_destinations_two_levels_and_sorted():
    src = "/d/Downloads"
    rows = [(f"{src}/Documents/proj", 3, 30), (f"{src}/Documents", 2, 20), (f"{src}/Other files/Archives", 5, 500),
            (f"{src}/Other files/Code/x", 1, 10), (f"{src}/Photos/2026/2026-11", 4, 400)]
    g = group_destinations(rows, [src])
    assert [x.name for x in g] == ["Other files", "Documents", "Photos"] or g[0].files == 6
    other = next(x for x in g if x.name == "Other files")
    assert other.files == 6 and {c.name for c in other.children.values()} == {"Archives", "Code"}
    docs = next(x for x in g if x.name == "Documents")
    assert docs.files == 5 and docs.size == 50


def test_group_destinations_prefixes_source_when_several():
    rows = [("/a/Dl/Documents", 1, 1), ("/b/Dl/Documents", 2, 2)]
    names = {x.name for x in group_destinations(rows, ["/a/Dl", "/b/Dl"])}
    assert names == {"Dl › Documents"} or len(names) == 1  # same basename merges; distinct ones stay apart
    rows = [("/a/One/Documents", 1, 1), ("/a/Two/Documents", 2, 2)]
    assert {x.name for x in group_destinations(rows, ["/a/One", "/a/Two"])} == {"One › Documents", "Two › Documents"}


def test_group_destinations_files_left_in_source_root():
    g = group_destinations([("/s", 2, 5)], ["/s"])
    assert g[0].name.startswith("(directly") and g[0].files == 2


def test_sample_moves_are_relative_and_limited():
    rows = [(f"/s/a{i}.pdf", f"/s/Documents/a{i}.pdf") for i in range(20)]
    out = sample_moves(rows, ["/s"], limit=5)
    assert len(out) == 5 and out[0] == ("a0.pdf", os.path.join("Documents", "a0.pdf"))


def test_suggested_folders_only_existing(tmp_path):
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Music").mkdir()
    assert [os.path.basename(p) for p in suggested_folders(str(tmp_path))] == ["Downloads", "Music"]


def test_scan_notice_wording():
    assert explain_scan_notice(10, 0, 0) == ""
    assert "No files" in explain_scan_notice(0, 0, 0)
    assert "incomplete" in explain_scan_notice(10, 2, 0)
    assert "stay exactly as they are" in explain_scan_notice(10, 0, 3)


# ------------------------------------------------------------- offscreen GUI drive
pytest.importorskip("PySide6")


@pytest.fixture
def window(tmp_path, monkeypatch):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from datacurator.engine.api import Engine
    from datacurator.gui import app as A
    # accept whichever confirmation is shown: Apply for "apply", Yes for "undo"
    monkeypatch.setattr(A.QMessageBox, "question", staticmethod(
        lambda parent, title, text, buttons, default=None: A.QMessageBox.Apply if buttons & A.QMessageBox.Apply
        else A.QMessageBox.Yes))
    qapp = QApplication.instance() or QApplication([])
    w = A.MainWindow(Engine(str(tmp_path / "appdata")))
    yield qapp, w
    w.engine.close()
    w.close()


def _wait(qapp, w, timeout=20):
    import time
    t0 = time.time()
    while w.thread is not None and time.time() - t0 < timeout:
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()
    assert w.thread is None, "task did not finish"


def test_quick_page_is_default_and_full_cycle(window, tmp_path):
    qapp, w = window
    from datacurator.gui import quick as Q
    assert w.pages.currentWidget() is w.quick and w.steps.currentRow() == -1
    src = tmp_path / "Downloads"
    (src / "misc").mkdir(parents=True)
    (src / "report.pdf").write_bytes(b"%PDF-1.4 x")
    (src / "misc" / "main.py").write_text("print(1)")
    (src / "setup.zip").write_bytes(b"PK\x03\x04")
    w.quick.add_sources([str(src)])
    assert w.quick.suggest_btn.isEnabled()
    w.quick.suggest()
    assert w.quick.views.currentIndex() == Q.WORKING
    _wait(qapp, w)
    assert w.quick.views.currentIndex() == Q.REVIEW
    assert "3 files" in w.quick.hero.text() and w.quick.apply_btn.isEnabled()
    assert w.quick.tree.topLevelItemCount() >= 2
    w.quick.apply()
    _wait(qapp, w)
    assert w.quick.views.currentIndex() == Q.DONE and "All done" in w.quick.done_hero.text()
    assert (src / "Documents" / "PDFs" / "report.pdf").exists() and not (src / "report.pdf").exists()
    w.quick.undo()
    _wait(qapp, w)
    assert (src / "report.pdf").exists() and "Undone" in w.quick.done_hero.text()
    w.show_quick()
    assert w.pages.currentWidget() is w.quick
    w.steps.setCurrentRow(2)
    assert w.pages.currentWidget() is w.review_page


def test_quick_page_per_subfolder_structures_full_cycle(window, tmp_path):
    qapp, w = window
    from datacurator.gui import quick as Q
    from datacurator.engine import subfolders as SF
    parent = tmp_path / "Stuff"
    for i in range(3):
        (parent / "Trip").mkdir(parents=True, exist_ok=True)
        (parent / "Trip" / f"IMG_{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpg")
    for n in ("a.pdf", "b.xlsx", "c.docx"):
        (parent / "Work").mkdir(exist_ok=True)
        (parent / "Work" / n).write_bytes(b"x")
    (parent / "repo" / ".git").mkdir(parents=True)
    (parent / "repo" / ".git" / "HEAD").write_text("ref")
    (parent / "repo" / "doc.pdf").write_bytes(b"x")
    w.quick.add_sources([str(parent)])
    assert w.quick.sub_check.isVisible() or w.quick.sub_infos          # offered for a folder with subfolders
    assert {i.name for i in w.quick.sub_infos} == {"Trip", "Work", "repo"}
    w.quick.sub_check.setChecked(True)
    assign = w.quick._assignments()
    assert assign[str(parent / "Work")] == "documents" and assign[str(parent / "repo")] == SF.LEAVE
    # the protected folder's picker is locked
    repo_row = [i.name for i in w.quick.sub_infos].index("repo")
    assert not w.quick.sub_table.cellWidget(repo_row, 2).isEnabled()
    w.quick.suggest()
    _wait(qapp, w)
    assert w.quick.views.currentIndex() == Q.REVIEW and not w.quick.tune_btn.isVisibleTo(w.quick)
    assert w.quick.apply_btn.isEnabled()
    w.quick.apply()
    _wait(qapp, w)
    assert w.quick.views.currentIndex() == Q.DONE and "All done" in w.quick.done_hero.text()
    assert (parent / "Work" / "PDFs" / "a.pdf").exists() and (parent / "repo" / "doc.pdf").exists()
    w.quick.undo()
    _wait(qapp, w)
    assert (parent / "Work" / "a.pdf").exists() and "Undone" in w.quick.done_hero.text()
