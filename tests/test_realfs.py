"""VERIFIED tests on real mounted filesystems (not simulated).

Enable with DATACURATOR_REALFS="name=/mount/point,..." (see scripts/realfs_setup.sh).
Each target gets: an active capability probe, a cross-volume organize from the
system disk with content verification, an idempotent re-plan, an in-place
rename pass (case-only rename and a rename cycle on that filesystem) and undo.
Results are written to tests/_realfs_results.json for the validation report.
"""
import hashlib
import json
import os
import shutil
import time
import uuid

import pytest

from conftest import profile
from fixtures import make_audio, make_file
from datacurator.models import PreflightError
from datacurator.storage.capabilities import probe

TARGETS = dict(kv.split("=", 1) for kv in os.environ.get("DATACURATOR_REALFS", "").split(",") if "=" in kv)
RESULTS = os.path.join(os.path.dirname(__file__), "_realfs_results.json")

pytestmark = pytest.mark.realfs


def _hashes(root):
    out = {}
    for dp, _, fns in os.walk(root):
        for f in fns:
            with open(os.path.join(dp, f), "rb") as fh:
                out[os.path.relpath(os.path.join(dp, f), root)] = hashlib.blake2b(fh.read()).hexdigest()
    return out


def _record(name, data):
    allr = {}
    if os.path.exists(RESULTS):
        allr = json.load(open(RESULTS))
    allr[name] = data
    json.dump(allr, open(RESULTS, "w"), indent=1, sort_keys=True)


def _fixture_library(lib):
    make_audio(str(lib / "rip/a.flac"), title="Café del Mar", artist="Beyoncé", album="Live: 2003?",
               albumartist="Beyoncé", tracknumber="1", date="2003")
    make_audio(str(lib / "rip/b.mp3"), ".mp3", title="aux", artist="Beyoncé", album="Live: 2003?",
               albumartist="Beyoncé", tracknumber="2", date="2003")
    make_file(str(lib / "rip/cover.jpg"), b"\xff\xd8" + os.urandom(2048))
    make_file(str(lib / "tv/Show.Name.S01E02.Title.1080p.mkv"), os.urandom(5 * 1024 * 1024))
    make_file(str(lib / "tv/Show.Name.S01E02.Title.1080p.en.forced.srt"), b"1\n00:00:01,000 --> 00:00:02,000\nHi\n")


@pytest.mark.skipif(not TARGETS, reason="DATACURATOR_REALFS not set")
@pytest.mark.parametrize("name", sorted(TARGETS))
def test_real_filesystem(name, engine, tmp_path):
    mount = TARGETS[name]
    base = os.path.join(mount, "curator-" + uuid.uuid4().hex[:6])
    os.makedirs(base)
    rec = {"mount": mount, "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        caps = probe(base)
        rec["probe"] = {k: getattr(caps, k) for k in ("fs_type", "case_sensitive", "normalization_insensitive",
                                                       "windows_naming", "supports_symlinks", "supports_hardlinks",
                                                       "supports_xattr", "is_network", "ascii_only")}
        lib = tmp_path / "lib"
        _fixture_library(lib)
        before = sorted(_hashes(lib).values())
        dest = os.path.join(base, "Library")
        p = profile([lib], music={"root": os.path.join(dest, "Music")}, tv={"root": os.path.join(dest, "TV")})
        engine.scan(p)
        store = engine.plan(p)
        assert store.count("changes") == 5
        res = engine.apply(store)
        assert res.status == "completed", res.errors
        rec["cross_volume_apply"] = f"{res.done} files, {res.bytes_copied} bytes verified"
        after = _hashes(dest)
        assert sorted(after.values()) == before, "verified copies retain identical content"
        want = "Music/Beyoncé/Live - 2003/01 - Café del Mar.flac"
        if caps.ascii_only:
            want = "Music/Beyonce/Live - 2003/01 - Cafe del Mar.flac"
        assert want in {k.replace(os.sep, "/") for k in after}
        assert "TV/Show Name/Season 01/Show Name - S01E02 - Title.en.forced.srt" in {k.replace(os.sep, "/") for k in after}
        # idempotent: organizing the destination in place changes nothing
        p2 = profile([dest + "/Music"], music={"root": ""})
        p2["sources"] = [os.path.join(dest, "Music")]
        engine.scan(p2)
        again = engine.plan(p2)
        assert again.count("changes") == 0, [i.reason for i in again.query("action='move'")]
        rec["replan_changes"] = 0
        # in-place renames on this filesystem: case-only + cycle
        work = os.path.join(base, "inplace")
        make_audio(os.path.join(work, "01 - one.flac"), title="One", artist="A", album="B", albumartist="A",
                   tracknumber="1", salt="x")
        make_audio(os.path.join(work, "03 - Three.flac"), title="Two", artist="A", album="B", albumartist="A",
                   tracknumber="2", salt="y")
        make_audio(os.path.join(work, "02 - Two.flac"), title="Three", artist="A", album="B", albumartist="A",
                   tracknumber="3", salt="z")
        h0 = _hashes(work)
        p3 = profile([work], music={"mode": "rename"})
        engine.scan(p3)
        st3 = engine.plan(p3)
        try:
            r3 = engine.apply(st3)
        except PreflightError as e:
            # In-place previews never write into sources, so a FUSE filesystem whose behaviour
            # cannot be inferred passively is measured at apply time; the user previews again.
            assert "behaves differently" in str(e)
            rec["inplace_needed_second_preview"] = True
            st3 = engine.plan(p3)
            r3 = engine.apply(st3)
        assert r3.done == 3, r3.errors
        names = sorted(os.listdir(work))
        assert names == ["01 - One.flac", "02 - Two.flac", "03 - Three.flac"], names
        rec["case_only_and_cycle"] = "ok"
        u = engine.undo(r3.batch_id)
        assert u.complete, u.summary()
        assert _hashes(work) == h0
        u = engine.undo(res.batch_id)
        assert u.complete, u.summary()
        assert sorted(_hashes(lib).values()) == before
        rec["undo"] = "complete"
        rec["result"] = "pass"
    except BaseException as e:
        rec["result"] = f"fail: {e}"
        raise
    finally:
        _record(name, rec)
        shutil.rmtree(base, ignore_errors=True)
