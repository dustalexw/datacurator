"""Folders and files that depend on staying where they are, and files that only work together."""
import os

import pytest

from fixtures import make_file, make_photo, snapshot
from datacurator import profiles as P
from datacurator.engine.exclusions import DirectoryExclusions
from datacurator.engine.filesets import find_sets, follow_name


def plan(engine, profile):
    stats = engine.scan(profile)
    return stats, engine.plan(profile)


def left_alone(store, path):
    item = store.by_source(str(path))
    return item is None or item.action in ("skip", "review")


# ---------------------------------------------------------------- dependent folders
@pytest.mark.parametrize("layout, why", [
    ({"site/.git/HEAD": "ref", "site/images/logo.png": "png"}, "Software project (Git)"),
    ({"app/package.json": "{}", "app/assets/data.csv": "a,b"}, "Software project"),
    ({"blog/index.html": "<html>", "blog/css/site.css": "body{}", "blog/photo.jpg": "jpg"}, "Website"),
    ({"Song Project/Song.als": "x", "Song Project/Samples/kick.wav": "x"}, "Ableton Live project"),
    ({"Song Project/Ableton Project Info/x.cfg": "x", "Song Project/drums.aif": "x"}, "Ableton Live project"),
    ({"Edit/Edit.prproj": "x", "Edit/Footage/clip.mov": "x"}, "Premiere Pro project"),
    ({"Paper/main.tex": "x", "Paper/figures/plot.pdf": "x"}, "LaTeX project"),
    ({"Old Photos.photoslibrary/originals/a.jpg": "x"}, "Photos library"),
    ({"Movie Disc/VIDEO_TS/VTS_01_1.VOB": "x"}, "DVD video"),
    ({"Win VM/Win.vmx": "x", "Win VM/disk.vmdk": "x"}, "Virtual machine"),
    ({"Phone backup/Manifest.db": "x", "Phone backup/Info.plist": "x", "Phone backup/Status.plist": "x",
      "Phone backup/00/abc": "x"}, "iPhone or iPad backup"),
    ({"Books/metadata.db": "x", "Books/metadata_db_prefs_backup.json": "{}", "Books/A/b.epub": "x"},
     "Calibre library"),
    ({".config/app/settings.json": "{}"}, "Hidden app or settings folder"),
    ({"tool/.venv/pyvenv.cfg": "x", "tool/run.py": "print()"}, "Software project"),
])
def test_dependent_folders_are_left_in_place(engine, lib, layout, why):
    paths = [make_file(str(lib / rel), content) for rel, content in layout.items()]
    before = snapshot(str(lib))
    stats, store = plan(engine, P.opening_profile([str(lib)]))
    reasons = {r for _, r in stats.excluded_folders}
    for path in paths:
        item = store.by_source(path)
        assert item is None or (item.action == "skip" and "excluded" in item.flags), (path, item and item.reason)
        if item is not None:
            reasons.add(item.reason)
    assert any(why in r for r in reasons), reasons
    result = engine.apply(store)
    assert result.failed == 0
    assert snapshot(str(lib)) == before or all(os.path.exists(p) for p in paths)
    assert all(os.path.exists(p) for p in paths)


def test_weak_signals_never_freeze_a_whole_source(engine, lib):
    """A stray Makefile, project file or index.html in Downloads must not protect Downloads."""
    stray = [make_file(str(lib / n)) for n in ("Makefile", "package.json", "index.html", "styles.css")]
    zipped = make_file(str(lib / "photos.zip"))
    stats, store = plan(engine, P.opening_profile([str(lib)]))
    assert not stats.excluded_folders
    assert store.by_source(zipped).dest == str(lib / "Other files/Archives/photos.zip")
    assert store.by_source(stray[0]).action == "move"


def test_strong_signal_protects_a_source_and_organize_anyway_lifts_it(engine, lib):
    make_file(str(lib / ".git/HEAD"), "ref")
    doc = make_file(str(lib / "notes.pdf"))
    p = P.opening_profile([str(lib)])
    stats, store = plan(engine, p)
    assert store.by_source(doc).action == "skip" and "Git" in store.by_source(doc).reason
    p["scan"]["allow_dirs"] = [str(lib)]
    stats, store = plan(engine, p)
    assert store.by_source(doc).dest == str(lib / "Documents/notes.pdf")
    # the repository's own data folder is still never touched
    assert (str(lib / ".git"), "Git repository data") in stats.excluded_folders
    assert store.by_source(str(lib / ".git/HEAD")) is None


def test_source_inside_a_library_is_protected(engine, tmp_path):
    itunes = tmp_path / "iTunes"
    make_file(str(itunes / "iTunes Library.itl"))
    song = make_file(str(itunes / "iTunes Media/Music/Band/Album/01 Song.pdf"))
    source = itunes / "iTunes Media"
    stats, store = plan(engine, P.opening_profile([str(source)]))
    assert left_alone(store, song)
    assert any("iTunes library" in r for _, r in stats.excluded_folders) or "iTunes" in store.by_source(song).reason


def test_home_folder_dotfiles_and_library_are_not_moved(engine, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    files = [make_file(str(home / n)) for n in (".zshrc", ".ssh/config", "Library/Preferences/a.plist",
                                                    "Library/Application Support/App/db.sqlite")]
    make_file(str(home / ".git/HEAD"))  # a dotfiles repository in the home folder
    loose = make_file(str(home / "loose.zip"))
    stats, store = plan(engine, P.opening_profile([str(home)]))
    for f in files:
        assert left_alone(store, f), f
    assert store.by_source(loose).action == "move"  # the home folder itself is not "a project"


def test_protection_can_be_turned_off_and_explicit_exclusions_still_win(tmp_path):
    make_file(str(tmp_path / "src/proj/.git/HEAD"))
    off = DirectoryExclusions({"exclude_games": False, "exclude_dirs": [str(tmp_path / "src/keep")]},
                              roots=[str(tmp_path / "src")])
    assert off.reason(str(tmp_path / "src/proj")) is None
    assert off.reason(str(tmp_path / "src/keep/sub")) == "Excluded folder"
    on = DirectoryExclusions({}, roots=[str(tmp_path / "src")])
    assert on.reason(str(tmp_path / "src/proj/deep")) == "Software project (Git)"
    assert on.protected_root(str(tmp_path / "src/proj/deep")) == (str(tmp_path / "src/proj"), "Software project (Git)")


# ---------------------------------------------------------------------- anchored files
def test_anchored_files_stay_put(engine, lib):
    page = make_file(str(lib / "Recipe.html"), "<html>")
    page_files = make_file(str(lib / "Recipe_files/style.css"), "x")
    project = make_file(str(lib / "Mix.als"))
    hidden = make_file(str(lib / ".env"))
    partial = make_file(str(lib / "big-movie.mkv.crdownload"))
    stats, store = plan(engine, P.opening_profile([str(lib)]))
    for path in (page, project, hidden, partial):
        item = store.by_source(path)
        assert item.action == "skip" and "excluded" in item.flags, (path, item.reason)
    assert left_alone(store, page_files)
    assert "Saved web page" in store.by_source(page).reason


# ------------------------------------------------------------------------- file sets
def test_find_sets_detects_archives_images_shapefiles_and_sidecars(tmp_path):
    names = ["movie.part1.rar", "movie.part2.rar", "movie.part10.rar", "old.rar", "old.r00", "old.r01",
             "data.7z.001", "data.7z.002", "split.zip", "split.z01", "roads.shp", "roads.dbf", "roads.shx",
             "roads.prj", "IMG_1.CR2", "IMG_1.JPG", "IMG_1.xmp", "IMG_2.HEIC", "IMG_2.AAE", "game.cue",
             "game (Track 1).bin", "game (Track 2).bin", "lonely.001", "solo.txt"]
    for n in names:
        make_file(str(tmp_path / n))
    (tmp_path / "game.cue").write_text('FILE "game (Track 1).bin" BINARY\n  TRACK 01 MODE1/2352\n'
                                       'FILE "game (Track 2).bin" BINARY\n  TRACK 02 AUDIO\n')
    sets = find_sets([str(tmp_path / n) for n in names])
    lead = {os.path.basename(k): os.path.basename(v.leader) for k, v in sets.items()}
    assert lead == {"movie.part2.rar": "movie.part1.rar", "movie.part10.rar": "movie.part1.rar",
                    "old.r00": "old.rar", "old.r01": "old.rar", "data.7z.002": "data.7z.001",
                    "split.z01": "split.zip", "roads.dbf": "roads.shp", "roads.shx": "roads.shp",
                    "roads.prj": "roads.shp", "IMG_1.xmp": "IMG_1.CR2", "IMG_2.AAE": "IMG_2.HEIC",
                    "game (Track 1).bin": "game.cue", "game (Track 2).bin": "game.cue"}
    assert not sets[str(tmp_path / "game (Track 1).bin")].renamable


def test_follow_name():
    assert follow_name("/a/IMG_1.xmp", "/a/IMG_1.jpg", "/b/2021-07 IMG_1.jpg") == "2021-07 IMG_1.xmp"
    assert follow_name("/a/x.part2.rar", "/a/x.part1.rar", "/b/x (2).part1.rar") == "x (2).part2.rar"
    assert follow_name("/a/same.dbf", "/a/same.shp", "/b/same.shp") == "same.dbf"


def test_sets_move_together_apply_and_undo(engine, lib):
    parts = [make_file(str(lib / "dl/movie.part1.rar"), b"1"), make_file(str(lib / "dl/movie.part2.rar"), b"2")]
    shape = [make_file(str(lib / "dl/roads.shp"), b"s"), make_file(str(lib / "dl/roads.dbf"), b"d")]
    photo = make_photo(str(lib / "dl/IMG_7.jpg"))
    side = make_file(str(lib / "dl/IMG_7.xmp"), b"<x:xmpmeta/>")
    p = P.opening_profile([str(lib)])
    stats, store = plan(engine, p)
    dest = {os.path.basename(s): store.by_source(s).dest for s in parts + shape + [photo, side]}
    assert os.path.dirname(dest["movie.part1.rar"]) == os.path.dirname(dest["movie.part2.rar"])
    assert dest["movie.part1.rar"] == str(lib / "Other files/Archives/dl/movie.part1.rar")
    assert os.path.dirname(dest["roads.shp"]) == os.path.dirname(dest["roads.dbf"]) == str(lib / "Other files/Data/dl")
    assert os.path.dirname(dest["IMG_7.xmp"]) == os.path.dirname(dest["IMG_7.jpg"])
    assert store.by_source(side).role == "companion"
    result = engine.apply(store)
    assert result.failed == 0 and result.done == 6
    assert all(os.path.exists(d) for d in dest.values())
    engine.scan(p)
    assert engine.plan(p).summary()["changes"] == 0  # re-planning an organized library changes nothing
    engine.undo(result.batch_id)
    assert all(os.path.exists(s) for s in parts + shape + [photo, side])


def test_a_set_whose_leader_stays_keeps_every_part(engine, lib):
    first = make_file(str(lib / "movie.part1.rar"), b"1")
    second = make_file(str(lib / "movie.part2.rar"), b"2")
    p = P.opening_profile([str(lib)])
    engine.scan(p)
    engine.catalog.set_excluded(first, True)
    store = engine.plan(p)
    assert store.by_source(first).action == "skip"
    assert store.by_source(second).action == "skip" and "movie.part1.rar" in store.by_source(second).reason


def test_set_member_collision_holds_the_leader(engine, lib):
    make_file(str(lib / "Other files/Data/roads.dbf"), b"different")
    shp = make_file(str(lib / "roads.shp"), b"s")
    dbf = make_file(str(lib / "roads.dbf"), b"d")
    store = plan(engine, P.opening_profile([str(lib)]))[1]
    assert store.by_source(dbf).action in ("review", "skip")
    assert store.by_source(shp).action in ("review", "skip")


def test_other_files_are_grouped_by_kind_and_keep_their_folder(engine, lib):
    files = {"setup.dmg": "Installers", "font.otf": "Fonts", "script.py": "Code", "disc.iso": "Disk images",
             "mystery.qqq": "Miscellaneous"}
    for name in files:
        make_file(str(lib / "Course" / name))
    store = plan(engine, P.opening_profile([str(lib)]))[1]
    for name, group in files.items():
        assert store.by_source(str(lib / "Course" / name)).dest == str(lib / "Other files" / group / "Course" / name)


def test_files_sorted_by_data_curator_never_look_like_a_project(engine, lib):
    """A loose package.json or .tex sorted into a category folder must not freeze that folder later."""
    loose = [make_file(str(lib / n)) for n in ("package.json", "thesis.tex", "Makefile", "index.html", "site.css")]
    p = P.opening_profile([str(lib)])
    stats, store = plan(engine, p)
    result = engine.apply(store)
    assert result.failed == 0 and result.done == len(loose)
    stats, store = plan(engine, p)
    assert not stats.excluded_folders
    assert store.summary()["changes"] == 0
    assert all(it.action == "none" for it in store.items())
