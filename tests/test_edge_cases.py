"""Cross-cutting edge cases: Unicode collisions, reserved names, long paths, dedupe, profiles."""
import os
import unicodedata

import pytest

from conftest import profile, rel
from fixtures import make_audio, make_file
from datacurator import profiles as P
from datacurator.models import CuratorError, PreflightError
from datacurator.storage.capabilities import Capabilities, inspect


def test_unicode_equivalent_names_share_one_folder_and_collide(engine, lib, tmp_path):
    nfc = unicodedata.normalize("NFC", "Beyoncé")
    nfd = unicodedata.normalize("NFD", "Beyoncé")
    make_audio(str(lib / "a/1.flac"), title="Halo", artist=nfc, album="I Am", albumartist=nfc, tracknumber="1", salt=1)
    make_audio(str(lib / "b/1.flac"), title="Halo", artist=nfd, album="I Am", albumartist=nfd, tracknumber="1", salt=2)
    make_audio(str(lib / "c/2.flac"), title="halo", artist=nfc, album="i am", albumartist=nfc.upper(), tracknumber="1",
               salt=3)
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    items = list(engine.plan(p).items())
    folders = {os.path.dirname(rel(i.dest, tmp_path / "M")) for i in items if i.dest}
    assert len(folders) == 1  # NFC/NFD/case variants never fork a folder
    assert sum(1 for i in items if i.action == "move") == 1
    assert sum(1 for i in items if "conflict" in i.flags) == 3


def test_reserved_names_and_trailing_dots_in_generated_paths(engine, lib, tmp_path):
    make_audio(str(lib / "x/1.flac"), title="AUX", artist="CON", album="Vol. 2.", albumartist="CON", tracknumber="1")
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    it = next(engine.plan(p).items())
    assert rel(it.dest, tmp_path / "M") == "CON_/Vol. 2/01 - AUX.flac"
    assert any("reserved" in r for c in it.sanitized for r in c["reasons"])


def test_long_names_and_paths_are_shortened_or_reviewed(engine, lib, tmp_path):
    long_title = "Very Long Title " * 30
    make_audio(str(lib / "x/1.flac"), title=long_title, artist="A", album="B", albumartist="A", tracknumber="1")
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    it = next(engine.plan(p).items())
    assert len(os.path.basename(it.dest).encode()) <= 255 and it.dest.endswith(".flac")
    caps = inspect(str(tmp_path / "M"))
    caps.max_path = len(str(tmp_path / "M")) + 30
    it = next(engine.plan(p, caps_override={str(tmp_path / "M"): caps}).items())
    assert len(it.dest) <= caps.max_path and it.dest.endswith(".flac")
    caps.max_path = len(str(tmp_path / "M")) + 8
    it = next(engine.plan(p, caps_override={str(tmp_path / "M"): caps}).items())
    assert it.action == "review" and "character limit" in it.reason


def test_apply_refuses_when_destination_behaves_differently_than_previewed(engine, lib, tmp_path):
    make_audio(str(lib / "x/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    dest = tmp_path / "M"
    dest.mkdir()
    fake = inspect(str(dest))
    fake.case_sensitive = False
    fake.windows_naming = True
    p = profile([lib], music={"root": str(dest)})
    engine.scan(p)
    store = engine.plan(p, caps_override={str(dest): fake})
    with pytest.raises(PreflightError) as e:
        engine.apply(store)
    assert "behaves differently" in str(e.value)
    # the measured behaviour is cached, so the next preview uses it and applies cleanly
    store = engine.plan(p)
    assert engine.apply(store).done == 1


def test_block_lossy_transfers(engine, lib, tmp_path):
    shm = "/dev/shm"
    if not os.access(shm, os.W_OK):
        pytest.skip("no second filesystem")
    import shutil
    import uuid
    dest = os.path.join(shm, "curator-" + uuid.uuid4().hex[:6])
    try:
        f = make_audio(str(lib / "x/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
        os.link(f, str(lib / "hardlink.bin"))
        p = profile([lib], music={"root": dest}, preservation={"block_lossy": True})
        engine.scan(p)
        it = next(i for i in engine.plan(p).items() if i.source == f)
        assert it.action == "skip" and "hard link" in it.reason
    finally:
        shutil.rmtree(dest, ignore_errors=True)


def test_dedupe_requires_approval_and_is_undoable(engine, lib, tmp_path):
    from datacurator.engine.dedupe import plan_dedupe
    make_file(str(lib / "a/x.bin"), b"same bytes")
    make_file(str(lib / "b/x.bin"), b"same bytes")
    make_file(str(lib / "c/y.bin"), b"other")
    p = profile([lib])
    engine.scan(p)
    q = tmp_path / "quarantine"
    store = plan_dedupe(engine.catalog, [str(lib)], str(q), engine.plans_dir)
    items = list(store.items())
    assert len(items) == 1 and items[0].source.endswith("b/x.bin")
    with pytest.raises(PreflightError):
        engine.apply(store)
    res = engine.apply(store, approve_deletes=True)
    assert res.done == 1 and (q / "library/b/x.bin").exists() and not (lib / "b/x.bin").exists()
    engine.undo(res.batch_id)
    assert (lib / "b/x.bin").read_bytes() == b"same bytes"


def test_unattended_requires_explicit_profile():
    prof = P.normalize({"sources": ["/tmp"]})
    with pytest.raises(CuratorError):
        P.check_unattended(prof)
    prof["unattended"] = {"enabled": True}
    with pytest.raises(CuratorError):
        P.check_unattended(prof)
    prof["unattended"]["conflict_policy"] = "skip"
    P.check_unattended(prof)


def test_profile_validation_reports_problems():
    with pytest.raises(CuratorError) as e:
        P.normalize({"categories": {"music": {"folder": "{nope}"}}, "conflict_policy": "overwrite"})
    assert "{nope}" in str(e.value) and "conflict_policy" in str(e.value)


def test_example_profiles_are_valid():
    d = os.path.join(os.path.dirname(__file__), "..", "profiles")
    files = [f for f in os.listdir(d) if f.endswith(".json")]
    assert files
    for f in files:
        P.load(os.path.join(d, f))


def test_catalog_tags_collections_search_without_folders(engine, lib):
    f = make_file(str(lib / "doc.pdf"), b"%PDF")
    engine.scan(profile([lib]))
    engine.catalog.add_tag(f, "taxes")
    engine.catalog.add_to_collection("2025 paperwork", f)
    assert engine.catalog.search(tag="taxes") == [f]
    assert engine.catalog.search(collection="2025 paperwork") == [f]
    assert engine.catalog.search(text="doc") == [f]
    assert sorted(os.listdir(lib)) == ["doc.pdf"]


def test_preview_tree_and_reports(engine, lib, tmp_path):
    make_audio(str(lib / "x/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    store = engine.plan(p)
    store.export_csv(str(tmp_path / "r.csv"))
    store.export_json(str(tmp_path / "r.json"))
    import json
    data = json.load(open(tmp_path / "r.json"))
    assert data["items"][0]["dest"].endswith("01 - T.flac")
    assert "destination" in open(tmp_path / "r.csv").readline()


def test_capabilities_probe_on_this_machine(tmp_path):
    from datacurator.storage.capabilities import probe
    c = probe(str(tmp_path))
    assert c.probed == "active" and c.free_bytes > 0
    assert not [f for f in os.listdir(tmp_path) if f.startswith(".curator-probe")]
    assert isinstance(Capabilities.from_dict(c.to_dict()), Capabilities)
