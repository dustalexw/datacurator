"""Music-library editing stays read-only until the reviewed plan is applied."""
import threading

import pytest

from conftest import profile
from fixtures import make_audio, snapshot
from datacurator.engine.music import FilenameMask, library_rows, save_details
from datacurator.metadata.audio import read_audio
from datacurator.models import CuratorError


def test_filename_pattern_preview_missing_only_and_exact_matches():
    row = {"path": "/library/Artist/Album/03 - A Song.flac", "root": "/library", "title": "Existing"}
    mask = FilenameMask("{album_artist}/{album}/{track:02} - {title}")
    assert mask.suggest(row) == {"album_artist": "Artist", "album": "Album", "track": "3"}
    assert mask.suggest(row, False)["title"] == "A Song"
    assert FilenameMask("{artist} - {title}").suggest({**row, "path": "/library/no separator.flac"}) == {}
    assert FilenameMask("{artist} - {album} - {track} - {title}").suggest(
        {"path": "/library/A - B - 07 - Song.mp3", "root": "/library"}) == {
            "artist": "A", "album": "B", "track": "7", "title": "Song"}


@pytest.mark.parametrize("mask", ["{artist}{title}", "{artist}/{artist}", "{wat}", "../{title}", "{title", "plain"])
def test_filename_pattern_rejects_ambiguous_or_invalid_masks(mask):
    with pytest.raises(CuratorError):
        FilenameMask(mask)


def test_library_scopes_sources_and_displays_overrides(engine, lib, tmp_path):
    a = lib / "A/Album/01.flac"
    b = tmp_path / "other/02.flac"
    make_audio(str(a), artist="A", album="Album", title="One", tracknumber="1", date="2000")
    make_audio(str(b), artist="B", album="Other", title="Two", tracknumber="2")
    engine.scan(profile([lib, b.parent]))
    engine.catalog.set_override(f"group:music:{a.parent}|album", "genre", "Jazz")
    save_details(engine.catalog, {str(a): {"album_artist": "Edited A", "year": "2024"}})
    rows = library_rows(engine.catalog, [str(lib), str(lib / "A")])
    assert len(rows) == 1 and rows[0]["album_artist"] == "Edited A"
    assert rows[0]["genre"] == "Jazz" and rows[0]["year"] == 2024
    assert rows[0]["edited"] and not rows[0]["missing"]
    cancelled = threading.Event()
    cancelled.set()
    assert library_rows(engine.catalog, [str(lib)], cancelled) is None
    assert library_rows(engine.catalog, []) == []


def test_batch_validation_is_atomic_and_does_not_touch_files(engine, lib):
    a = lib / "one.flac"
    make_audio(str(a), artist="A", title="One", album="B", tracknumber="1")
    engine.scan(profile([lib]))
    before = snapshot(str(lib))
    with pytest.raises(CuratorError):
        save_details(engine.catalog, {str(a): {"genre": "Rock"}, "missing.flac": {"album": "X"}})
    assert engine.catalog.overrides() == {}
    with pytest.raises(CuratorError):
        save_details(engine.catalog, {str(a): {"track": "abc"}})
    assert engine.catalog.overrides() == {}
    save_details(engine.catalog, {str(a): {"genre": "Jazz", "track": "03"}})
    assert snapshot(str(lib)) == before
    assert engine.catalog.overrides()[str(a)] == {"genre": "Jazz", "track": "3"}


def test_music_edits_preview_apply_write_tags_and_undo(engine, lib):
    source = lib / "original.flac"
    make_audio(str(source), title="Old", artist="A", album="B", albumartist="A", tracknumber="1")
    original = source.read_bytes()
    p = profile([lib], music={"preset": "music_in_source"}, write_tags=True)
    engine.scan(p)
    save_details(engine.catalog, {str(source): {"title": "New", "album": "New Album", "track": "2", "year": "2020"}})
    plan = engine.plan(p)
    target = lib / "Music/A/New Album/02 - New.flac"
    assert plan.by_source(str(source)).dest == str(target)
    assert any(item.action == "write_tags" for item in plan.items())
    assert source.read_bytes() == original and not target.exists()
    result = engine.apply(plan)
    assert result.status == "completed" and not source.exists()
    tags = read_audio(str(target))
    assert (tags["title"], tags["album"], tags["track"], tags["date"]) == ("New", "New Album", 2, "2020")
    undo = engine.undo(result.batch_id)
    assert not undo.conflicts and source.read_bytes() == original and not target.exists()


def test_music_edits_do_not_write_tags_without_opt_in(engine, lib):
    source = lib / "original.flac"
    make_audio(str(source), title="Old", artist="A", album="B", tracknumber="1")
    original = source.read_bytes()
    p = profile([lib], music={"preset": "music_in_source"})
    engine.scan(p)
    save_details(engine.catalog, {str(source): {"title": "New"}})
    plan = engine.plan(p)
    assert all(item.action != "write_tags" for item in plan.items())
    assert engine.apply(plan).status == "completed"
    assert (lib / "Music/A/B/01 - New.flac").read_bytes() == original
