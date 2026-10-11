"""Stage 3a: complete Artist–Album workflows."""
import os

from conftest import profile, rel
from fixtures import make_audio, make_file


def plan(engine, lib, dest=None, **music):
    m = {"root": str(dest) if dest else ""}
    m.update({k: v for k, v in music.items() if k not in ("top",)})
    p = profile([lib], music=m, **music.get("top", {}))
    engine.scan(p)
    store = engine.plan(p)
    return p, store, {os.path.relpath(i.source, lib): i for i in store.items()}


def dests(items, root):
    return {k: rel(v.dest, root) for k, v in items.items()}


def test_conflicting_album_artist_goes_to_review_with_suggestion(engine, lib):
    make_audio(str(lib / "x/1.flac"), title="A", artist="P", album="Alb", albumartist="Prince", tracknumber="1")
    make_audio(str(lib / "x/2.flac"), title="B", artist="P", album="Alb", albumartist="Prince", tracknumber="2")
    make_audio(str(lib / "x/3.flac"), title="C", artist="P", album="Alb", albumartist="The Artist", tracknumber="3")
    _, store, items = plan(engine, lib)
    assert all(i.action == "review" for i in items.values())
    it = items["x/1.flac"]
    assert "disagree" in it.reason and it.suggestions["album_artist"] == "Prince"
    assert not (lib / "Prince").exists()


def test_featured_artists_do_not_scatter_album(engine, lib):
    make_audio(str(lib / "x/1.flac"), title="One", artist="Main", album="LP", tracknumber="1")
    make_audio(str(lib / "x/2.flac"), title="Two", artist="Main feat. Guest", album="LP", tracknumber="2")
    make_audio(str(lib / "x/3.flac"), title="Three", artist="Main ft. Other", album="LP", albumartist="Main", tracknumber="3")
    _, _, items = plan(engine, lib)
    assert set(dests(items, lib).values()) == {"Main/LP/01 - One.flac", "Main/LP/02 - Two.flac", "Main/LP/03 - Three.flac"}
    # artist mode keeps the album together too (majority primary artist)
    _, _, items = plan(engine, lib, preset="artist/album")
    assert {os.path.dirname(d) for d in dests(items, lib).values()} == {"Main/LP"}


def test_compilations_kept_together_with_configurable_name(engine, lib):
    for i, artist in enumerate(["A", "B", "C"], 1):
        make_audio(str(lib / f"comp/{i}.mp3"), ".mp3", title=f"T{i}", artist=artist, album="Hits 99",
                   tracknumber=str(i), compilation=True)
    for i, artist in enumerate(["D", "E"], 1):
        make_audio(str(lib / f"ost/{i}.m4a"), ".m4a", title=f"S{i}", artist=artist, album="Film OST",
                   albumartist="VA", tracknumber=str(i))
    _, _, items = plan(engine, lib, compilation_name="Compilations")
    d = dests(items, lib)
    assert {os.path.dirname(v) for k, v in d.items() if k.startswith("comp")} == {"Compilations/Hits 99"}
    assert {os.path.dirname(v) for k, v in d.items() if k.startswith("ost")} == {"Compilations/Film OST"}
    _, _, items = plan(engine, lib, preset="artist/album")  # Artist mode: compilation still together
    assert {os.path.dirname(v) for k, v in dests(items, lib).items() if k.startswith("comp")} == {"Various Artists/Hits 99"}


def test_untagged_multi_artist_album_needs_review_then_override_fixes(engine, lib):
    for i, artist in enumerate(["A", "B", "C"], 1):
        make_audio(str(lib / f"mix/{i}.flac"), title=f"T{i}", artist=artist, album="Mixtape", tracknumber=str(i))
    p, store, items = plan(engine, lib)
    assert all(i.action == "review" for i in items.values())
    assert items["mix/1.flac"].suggestions.get("compilation") == "1"
    group = items["mix/1.flac"].group
    engine.catalog.set_override(f"group:{group}", "compilation", "1")
    store = engine.plan(p)
    assert sorted(rel(i.dest, lib) for i in store.items()) == [
        "Various Artists/Mixtape/01 - T1.flac", "Various Artists/Mixtape/02 - T2.flac", "Various Artists/Mixtape/03 - T3.flac"]


def test_multidisc_same_folder_and_disc_folders(engine, lib):
    make_audio(str(lib / "box/a.flac"), title="X", artist="A", album="Box", albumartist="A", tracknumber="1", discnumber="1/2")
    make_audio(str(lib / "box/b.flac"), title="Y", artist="A", album="Box", albumartist="A", tracknumber="1", discnumber="2/2")
    _, _, items = plan(engine, lib)
    assert set(dests(items, lib).values()) == {"A/Box/1-01 - X.flac", "A/Box/2-01 - Y.flac"}
    _, _, items = plan(engine, lib, preset="album_artist/album/disc")
    assert set(dests(items, lib).values()) == {"A/Box/Disc 01/01 - X.flac", "A/Box/Disc 02/01 - Y.flac"}


def test_multidisc_split_across_cd_folders_with_disc_in_album_title(engine, lib):
    make_audio(str(lib / "Album/CD1/01.flac"), title="X", artist="A", album="Big (Disc 1)", albumartist="A", tracknumber="1")
    make_audio(str(lib / "Album/CD2/01.flac"), title="Y", artist="A", album="Big (Disc 2)", albumartist="A", tracknumber="1")
    make_file(str(lib / "Album/CD1/cover.jpg"), b"same")
    make_file(str(lib / "Album/CD2/cover.jpg"), b"same")
    _, store, items = plan(engine, lib, root=str(lib / "out"))
    d = dests(items, lib / "out")
    assert d["Album/CD1/01.flac"] == "A/Big/1-01 - X.flac" and d["Album/CD2/01.flac"] == "A/Big/2-01 - Y.flac"
    assert items["Album/CD1/01.flac"].provenance["disc"] == "album title"
    covers = [items["Album/CD1/cover.jpg"], items["Album/CD2/cover.jpg"]]
    assert sorted(c.action for c in covers) == ["move", "skip"]  # identical artwork: one copy, other flagged duplicate
    assert any("duplicate" in c.flags for c in covers)


def test_single_disc_never_gets_disc_folder(engine, lib):
    make_audio(str(lib / "src/1.flac"), title="X", artist="A", album="B", albumartist="A", tracknumber="1", discnumber="1/1")
    _, _, items = plan(engine, lib, preset="album_artist/album/disc")
    assert set(dests(items, lib).values()) == {"A/B/01 - X.flac"}


def test_existing_folder_spelling_is_reused_not_forked(engine, lib):
    (lib / "radiohead").mkdir()
    make_audio(str(lib / "in/1.flac"), title="X", artist="Radiohead", album="OK", albumartist="Radiohead", tracknumber="1")
    _, _, items = plan(engine, lib)
    assert dests(items, lib)["in/1.flac"] == "radiohead/OK/01 - X.flac"


def test_missing_track_numbers_and_titles(engine, lib):
    make_audio(str(lib / "src/one.flac"), title="First", artist="A", album="B", albumartist="A")
    make_audio(str(lib / "src/02 Second.flac"), artist="A", album="B", albumartist="A", tracknumber="2")
    _, _, items = plan(engine, lib)
    d = dests(items, lib)
    assert d["src/one.flac"] == "A/B/First.flac"
    assert d["src/02 Second.flac"] == "A/B/02 - 02 Second.flac"
    assert "original filename" in items["src/02 Second.flac"].provenance["title"]


def test_singles_review_or_singles_folder(engine, lib):
    make_audio(str(lib / "s/x.flac"), title="Single", artist="Solo")
    _, _, items = plan(engine, lib)
    assert items["s/x.flac"].action == "review" and "No album" in items["s/x.flac"].reason
    _, _, items = plan(engine, lib, singles_folder="Singles")
    assert dests(items, lib)["s/x.flac"] == "Solo/Singles/Single.flac"


def test_different_releases_with_identical_titles(engine, lib):
    make_audio(str(lib / "gh1/1.flac"), title="Song", artist="Band", album="Greatest Hits", albumartist="Band",
               tracknumber="1", date="1990", salt=1)
    make_audio(str(lib / "gh2/1.flac"), title="Song", artist="Band", album="Greatest Hits", albumartist="Band",
               tracknumber="1", date="2005", salt=2)
    _, _, items = plan(engine, lib)
    assert set(dests(items, lib).values()) == {"Band/Greatest Hits (1990)/01 - Song.flac",
                                                "Band/Greatest Hits (2005)/01 - Song.flac"}


def test_identical_titles_without_distinguishing_year_go_to_review(engine, lib):
    make_audio(str(lib / "a/1.flac"), title="S", artist="Band", album="Live", albumartist="Band", tracknumber="1",
               salt=1, **{"musicbrainz_albumid": "aaaa"})
    make_audio(str(lib / "b/1.flac"), title="S", artist="Band", album="Live", albumartist="Band", tracknumber="1",
               salt=2, **{"musicbrainz_albumid": "bbbb"})
    _, _, items = plan(engine, lib)
    assert all(i.action == "review" for i in items.values())


def test_multiple_audio_versions(engine, lib):
    make_audio(str(lib / "a/t.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    make_audio(str(lib / "a/t.mp3"), ".mp3", title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    make_audio(str(lib / "b/t.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1", salt="remaster")
    _, _, items = plan(engine, lib)
    assert items["a/t.mp3"].dest.endswith("01 - T.mp3") and items["a/t.mp3"].action == "move"
    flacs = [items["a/t.flac"], items["b/t.flac"]]
    assert sorted(i.action for i in flacs) == ["move", "review"]
    assert all("conflict" in i.flags for i in flacs)


def test_unsupported_metadata_organized_only_after_accepting_folder_suggestions(engine, lib):
    make_file(str(lib / "Artist X/Some Album/01 - Intro.flac"), b"not really flac")
    p, store, items = plan(engine, lib)
    it = items["Artist X/Some Album/01 - Intro.flac"]
    assert it.action == "review" and it.suggestions["album_artist"] == "Artist X"
    for k, v in it.suggestions.items():
        engine.catalog.set_override(it.source, k, v)
    store = engine.plan(p)
    it = next(store.items())
    assert it.action == "none"  # already satisfies the rules: nothing to do
    assert (lib / "Artist X/Some Album/01 - Intro.flac").read_bytes() == b"not really flac"


def test_companions_follow_album_and_lyrics_renamed(engine, lib, tmp_path):
    make_audio(str(lib / "rip/track1.flac"), title="One", artist="A", album="B", albumartist="A", tracknumber="1")
    make_file(str(lib / "rip/track1.lrc"), "[00:01]la")
    make_file(str(lib / "rip/folder.jpg"), b"jpg")
    make_file(str(lib / "rip/booklet.pdf"), b"%PDF")
    make_file(str(lib / "rip/rip.log"), b"log")
    _, _, items = plan(engine, lib, tmp_path / "M")
    d = dests(items, tmp_path / "M")
    assert d == {"rip/track1.flac": "A/B/01 - One.flac", "rip/track1.lrc": "A/B/01 - One.lrc",
                 "rip/folder.jpg": "A/B/folder.jpg", "rip/booklet.pdf": "A/B/booklet.pdf", "rip/rip.log": "A/B/rip.log"}


CUE = '''REM GENRE Rock
PERFORMER "A"
TITLE "B"
FILE "{f}" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
'''


def test_cue_reference_flagged_or_rewritten_and_undoable(engine, lib, tmp_path):
    make_audio(str(lib / "rip/track1.flac"), title="One", artist="A", album="B", albumartist="A", tracknumber="1")
    make_file(str(lib / "rip/disc.cue"), CUE.format(f="track1.flac"))
    _, _, items = plan(engine, lib, tmp_path / "M")
    assert items["rip/disc.cue"].action == "review" and "track1.flac" in items["rip/disc.cue"].reason
    p, store, items = plan(engine, lib, tmp_path / "M", top={"companions": {"update_references": True}})
    assert items["rip/disc.cue"].action == "move"
    rw = [i for i in store.items() if i.action == "rewrite"]
    assert len(rw) == 1 and rw[0].payload["replace"] == {"track1.flac": "01 - One.flac"}
    res = engine.apply(store)
    assert res.status == "completed", res.errors
    cue = (tmp_path / "M/A/B/disc.cue").read_text()
    assert 'FILE "01 - One.flac" WAVE' in cue
    engine.undo(res.batch_id)
    assert (lib / "rip/disc.cue").read_text() == CUE.format(f="track1.flac")


def test_single_file_image_with_cue_keeps_its_filename(engine, lib, tmp_path):
    make_audio(str(lib / "img/Album Image.flac"), title="Whole", artist="A", album="B", albumartist="A")
    make_file(str(lib / "img/Album Image.cue"),
              CUE.format(f="Album Image.flac") + "  TRACK 02 AUDIO\n    INDEX 01 03:00:00\n")
    _, _, items = plan(engine, lib, tmp_path / "M")
    d = dests(items, tmp_path / "M")
    assert d["img/Album Image.flac"] == "A/B/Album Image.flac"
    assert d["img/Album Image.cue"] == "A/B/Album Image.cue"


def test_tag_writing_is_explicit_only(engine, lib, tmp_path):
    f = make_audio(str(lib / "x/1.flac"), title="T", artist="A", album="Wrong", albumartist="A", tracknumber="1")
    engine.catalog.set_override(f, "album", "Right")
    raw = open(f, "rb").read()
    p, store, items = plan(engine, lib, tmp_path / "M")
    assert items["x/1.flac"].dest.endswith("A/Right/01 - T.flac")
    assert not [i for i in store.items() if i.action == "write_tags"]
    res = engine.apply(store)
    out = tmp_path / "M/A/Right/01 - T.flac"
    assert out.read_bytes() == raw  # filename/folder changed, tags untouched
    engine.undo(res.batch_id)
    p, store, items = plan(engine, lib, tmp_path / "M", top={"write_tags": True})
    assert [i for i in store.items() if i.action == "write_tags"]
    res = engine.apply(store)
    assert res.status == "completed", res.errors
    import mutagen
    assert mutagen.File(str(out))["ALBUM"] == ["Right"]
    engine.undo(res.batch_id)
    assert open(f, "rb").read() == raw
