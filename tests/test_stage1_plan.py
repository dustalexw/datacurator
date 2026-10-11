"""Stage 1: scanning, catalog, rule engine and preview."""
import os

from conftest import plan_map, profile, rel
from fixtures import make_audio, make_file, make_photo, snapshot


def music_profile(src, dest=None, **music):
    m = {"enabled": True, "root": str(dest) if dest else ""}
    m.update(music)
    return profile([src], music=m)


def test_preview_does_not_mutate_sources(engine, lib, tmp_path):
    make_audio(str(lib / "in/a.flac"), ".flac", title="One", artist="A", album="Alb", albumartist="A", tracknumber="1")
    make_audio(str(lib / "in/b.mp3"), ".mp3", title="Two", artist="A", album="Alb", albumartist="A", tracknumber="2")
    make_file(str(lib / "in/cover.jpg"), b"\xff\xd8jpeg")
    make_file(str(lib / "tv/Show.S01E02.Title.720p.mkv"), b"video")
    make_file(str(lib / "tv/Show.S01E02.Title.720p.en.srt"), b"sub")
    before = snapshot(str(lib))
    p = profile([lib], music={"root": str(tmp_path / "Music")}, tv={"root": str(tmp_path / "TV")})
    engine.scan(p)
    store = engine.plan(p)
    assert store.count("changes") >= 4
    assert snapshot(str(lib)) == before
    assert not (tmp_path / "Music").exists() and not (tmp_path / "TV").exists()


def test_default_music_layout_and_formats(engine, lib, tmp_path):
    dest = tmp_path / "Music"
    exts = [".flac", ".mp3", ".m4a", ".alac.m4a", ".ogg", ".opus", ".wav", ".aiff", ".wma"]
    for i, ext in enumerate(exts, 1):
        real = ".m4a" if ext == ".alac.m4a" else ext
        make_audio(str(lib / f"rip/f{i}{real}"), ext, title=f"Song {i}", artist="Band", album="Record",
                   albumartist="Band", tracknumber=str(i), date="2001")
    p = music_profile(lib, dest)
    engine.scan(p)
    items = plan_map(engine.plan(p))
    got = sorted(rel(it.dest, dest) for it in items.values())
    want = sorted(f"Band/Record/{i:02d} - Song {i}{'.m4a' if e == '.alac.m4a' else e}" for i, e in enumerate(exts, 1))
    assert got == want
    fmts = {it.provenance.get("title") for it in items.values()}
    assert "tag:ALAC" in fmts and "tag:AAC" in fmts and "tag:WMA" in fmts


def test_presets(engine, lib, tmp_path):
    make_audio(str(lib / "x/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1", date="1999-05-01")
    dest = tmp_path / "M"
    for preset, expect in [("album_artist/year-album", "A/1999 - B/01 - T.flac"),
                           ("artist/album", "A/B/01 - T.flac"),
                           ("album_artist/album/disc", "A/B/01 - T.flac")]:
        p = music_profile(lib, dest, preset=preset)
        engine.scan(p)
        it = next(iter(plan_map(engine.plan(p)).values()))
        assert rel(it.dest, dest) == expect, preset


def test_determinism_same_input_same_plan(engine, lib, tmp_path):
    for i in range(6):
        make_audio(str(lib / f"d{i % 2}/{i}.flac"), title=f"T{i}", artist="A", album=f"B{i % 2}", albumartist="A",
                   tracknumber=str(i))
    make_file(str(lib / "Show.S01E01.mkv"), b"v")
    p = profile([lib], music={"root": str(tmp_path / "M")}, tv={"root": str(tmp_path / "T")})
    engine.scan(p)
    a = [(i.source, i.dest, i.action, i.reason) for i in engine.plan(p).items()]
    engine.scan(p)
    b = [(i.source, i.dest, i.action, i.reason) for i in engine.plan(p).items()]
    assert a == b


def test_incremental_scan_reuses_metadata(engine, lib):
    for i in range(5):
        make_audio(str(lib / f"{i}.flac"), title=str(i), artist="A", album="B")
    p = music_profile(lib)
    s1 = engine.scan(p)
    assert s1.extracted == 5
    s2 = engine.scan(p)
    assert s2.extracted == 0 and s2.reused == 5
    make_audio(str(lib / "0.flac"), title="changed", artist="A", album="B", salt=1)
    os.utime(lib / "0.flac", ns=(1, 1))
    s3 = engine.scan(p)
    assert s3.extracted == 1
    os.remove(lib / "1.flac")
    s4 = engine.scan(p)
    assert s4.removed == 1 and engine.catalog.count([str(lib)]) == 4


def test_documents_photos_and_minimal_folders(engine, lib, tmp_path):
    make_file(str(lib / "ProjectX/spec.pdf"), b"%PDF")
    make_file(str(lib / "loose.docx"), b"doc")
    make_photo(str(lib / "cam/img1.jpg"), "2021:07:04 10:00:00")
    make_photo(str(lib / "cam/img2.jpg"), None)
    p = profile([lib], documents={"enabled": True, "root": str(tmp_path / "Docs")},
                photos={"enabled": True, "root": str(tmp_path / "Photos")})
    engine.scan(p)
    store = engine.plan(p)
    items = {os.path.basename(i.source): i for i in store.items()}
    assert rel(items["spec.pdf"].dest, tmp_path / "Docs") == "ProjectX/spec.pdf"
    assert rel(items["loose.docx"].dest, tmp_path / "Docs") == "loose.docx"
    assert rel(items["img1.jpg"].dest, tmp_path / "Photos") == "2021/2021-07/img1.jpg"
    assert items["img2.jpg"].action == "review"  # no capture date: never invented
    new_dirs = store.header("new_dirs")
    # only folders that receive files (plus their parents) are created
    assert sorted(rel(d, tmp_path) for d in new_dirs) == sorted(
        ["Docs", "Docs/ProjectX", "Photos", "Photos/2021", "Photos/2021/2021-07"])


def test_organize_within_existing_root_no_wrapper(engine, lib):
    make_audio(str(lib / "messy/x.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    p = music_profile(lib)  # no root: organize inside the source root itself
    engine.scan(p)
    it = next(iter(engine.plan(p).items()))
    assert rel(it.dest, lib) == "A/B/01 - T.flac"


def test_already_organized_is_unchanged(engine, lib):
    make_audio(str(lib / "A/B/01 - T.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    p = music_profile(lib)
    engine.scan(p)
    it = next(iter(engine.plan(p).items()))
    assert it.action == "none"


def test_sort_only_and_rename_only(engine, lib, tmp_path):
    make_audio(str(lib / "in/track.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="4")
    p = music_profile(lib, tmp_path / "M", mode="move")
    engine.scan(p)
    assert rel(next(engine.plan(p).items()).dest, tmp_path / "M") == "A/B/track.flac"
    p = music_profile(lib, tmp_path / "M", mode="rename")
    assert rel(next(engine.plan(p).items()).dest, lib) == "in/04 - T.flac"


def test_sanitization_shown_in_preview(engine, lib, tmp_path):
    make_audio(str(lib / "x.flac"), title="What?", artist="AC/DC", album="Live: 1991", albumartist="AC/DC",
               tracknumber="1")
    p = music_profile(lib, tmp_path / "M")
    engine.scan(p)
    it = next(engine.plan(p).items())
    assert rel(it.dest, tmp_path / "M") == "AC-DC/Live - 1991/01 - What.flac"
    assert "sanitized" in it.flags and len(it.sanitized) == 3


def test_symlink_loop_is_detected(engine, lib):
    (lib / "a").mkdir()
    make_audio(str(lib / "a/x.flac"), title="T", artist="A", album="B")
    os.symlink(str(lib), str(lib / "a/loop"))
    p = music_profile(lib)
    p["scan"] = {"follow_symlinks": True}
    st = engine.scan(p)
    assert st.cycles, "cycle must be reported"
    assert engine.catalog.count([str(lib)]) == 1
    p["scan"] = {"follow_symlinks": False}
    st = engine.scan(p)
    assert st.skipped_symlinks == 1 and not st.cycles
