"""Per-subfolder preset structures: suggestion, planning, protection, apply, stability and undo."""
import os

import pytest

from fixtures import make_audio, make_file, make_photo, snapshot
from datacurator import profiles as P
from datacurator.engine import subfolders as S
from datacurator.models import CuratorError


@pytest.fixture
def parent(lib):
    for i, (t, trk) in enumerate([("One", 1), ("Two", 2)]):
        make_audio(str(lib / "Tunes" / f"x{i}.flac"), title=t, artist="Band", album="Record", albumartist="Band",
                   tracknumber=str(trk))
    for i in range(3):
        make_photo(str(lib / "Trip" / f"IMG_{i}.jpg"), date=f"2021:07:0{i + 1} 10:00:00")
    for rel in ("Work/report.pdf", "Work/budget.xlsx", "Work/notes.docx", "Mixed/setup.dmg", "Mixed/a.zip",
                "Mixed/readme.txt", "repo/.git/HEAD", "repo/README.md", "repo/spec.pdf", "loose.pdf"):
        make_file(str(lib / rel))
    return lib


def files_only(parent):
    """Every file with its content and mtime; folder mtimes legitimately change when entries move."""
    return {k: v for k, v in snapshot(str(parent)).items() if not k.endswith("/")}


def info_map(parent):
    return {i.name: i for i in S.inspect_all(str(parent))}


def test_suggestions_follow_content_and_protect_dependent_folders(parent):
    m = info_map(parent)
    assert m["Tunes"].layout == "music" and m["Trip"].layout == "photos"
    assert m["Work"].layout == "documents" and m["Mixed"].layout == "tidy"
    assert m["repo"].protected and m["repo"].layout == "leave"


def test_empty_subfolder_is_left_alone(lib):
    (lib / "Empty").mkdir()
    assert info_map(lib)["Empty"].layout == "leave"


def test_unknown_layout_is_rejected(engine, parent):
    with pytest.raises(CuratorError):
        S.plan_groups(engine, str(parent), {str(parent / "Work"): "nonsense"})


def test_each_subfolder_gets_its_own_structure_and_protected_ones_stay(engine, parent):
    assign = {i.path: i.layout for i in S.inspect_all(str(parent))}
    before_repo = snapshot(str(parent / "repo"))
    groups = S.plan_groups(engine, str(parent), assign)
    assert [g.layout for g in groups] == ["tidy", "music", "photos", "documents"]
    dests = {os.path.relpath(it.source, str(parent)): os.path.relpath(it.dest, str(parent))
             for g in groups for it in g.store.items() if it.action == "move"}
    assert dests["Tunes/x0.flac"] == "Tunes/Band/Record/01 - One.flac"
    assert dests["Trip/IMG_0.jpg"] == "Trip/2021/2021-07/IMG_0.jpg"
    assert dests["Work/report.pdf"] == "Work/PDFs/report.pdf"
    assert dests["Work/budget.xlsx"] == "Work/Spreadsheets/budget.xlsx"
    assert dests["Mixed/setup.dmg"] == "Mixed/Other files/Installers/setup.dmg"
    assert not any(k.startswith("repo/") for k in dests) and "loose.pdf" not in dests
    res = S.apply_groups(engine, groups)
    assert res.failed == 0 and res.status == "completed" and len(res.batches) == 4
    assert snapshot(str(parent / "repo")) == before_repo and (parent / "loose.pdf").exists()
    # structure is stable: planning again proposes nothing
    again = S.plan_groups(engine, str(parent), assign)
    assert sum(g.store.count("changes") for g in again) == 0


def test_undo_restores_everything(engine, parent):
    before = files_only(parent)
    assign = {i.path: i.layout for i in S.inspect_all(str(parent))}
    res = S.apply_groups(engine, S.plan_groups(engine, str(parent), assign))
    assert files_only(parent) != before
    reports = S.undo_groups(engine, res.batches)
    assert all(r.complete for r in reports)
    assert files_only(parent) == before


def test_loose_files_in_the_parent_can_get_a_preset_without_touching_subfolders(engine, parent):
    groups = S.plan_groups(engine, str(parent), {}, loose="documents")
    assert len(groups) == 1 and groups[0].sources == [str(parent)]
    moves = {os.path.relpath(it.source, str(parent)) for it in groups[0].store.items() if it.action == "move"}
    assert moves == {"loose.pdf"}
    S.apply_groups(engine, groups)
    assert (parent / "PDFs" / "loose.pdf").exists() and (parent / "Work" / "report.pdf").exists()


def test_leave_everything_plans_nothing(engine, parent):
    assert S.plan_groups(engine, str(parent), {i.path: "leave" for i in S.inspect_all(str(parent))}) == []


def test_layout_table_matches_builders():
    for key in S.P.SUBFOLDER_LAYOUTS:
        if key != "leave":
            assert P.subfolder_profile(key, ["/tmp/x"])["sources"] == [os.path.abspath("/tmp/x")]
