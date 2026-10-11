"""Stage 2: safe execution, journaling, recovery and undo.

Storage failures here are SIMULATED through a fault-injecting storage adapter
(FaultFS). Cross-volume tests use a real second filesystem (tmpfs at /dev/shm)
when available.
"""
import errno
import os
import shutil
import uuid

import pytest

from conftest import profile, rel
from fixtures import make_audio, make_file, snapshot
from datacurator.engine.executor import Executor, recover
from datacurator.engine.undo import undo_batch
from datacurator.models import PreflightError
from datacurator.storage.fsops import LocalFS


class SimulatedCrash(BaseException):
    """Stands in for power loss / process kill: nothing after it runs."""


class FaultFS(LocalFS):
    def __init__(self):
        self.crash_after_bytes = None
        self.write_errno = None
        self.rename_errnos: list[int] = []
        self.crash_on_unlink_of = None
        self.on_write = None
        self.renames = 0

    def open_write(self, p, append=False):
        f = super().open_write(p, append)
        fs = self

        class W:
            def __init__(self):
                self.n = 0
                self.closed = False

            def write(self, b):
                if fs.write_errno:
                    raise OSError(fs.write_errno, os.strerror(fs.write_errno))
                if fs.crash_after_bytes is not None and self.n + len(b) > fs.crash_after_bytes:
                    f.write(b[: max(0, fs.crash_after_bytes - self.n)])
                    f.flush()
                    raise SimulatedCrash()
                self.n += len(b)
                if fs.on_write:
                    fs.on_write()
                return f.write(b)

            def __getattr__(self, k):
                return getattr(f, k)

            def close(self):
                self.closed = True
                f.close()
        return W()

    def rename_noreplace(self, src, dst):
        self.renames += 1
        if self.rename_errnos:
            e = self.rename_errnos.pop(0)
            raise OSError(e, os.strerror(e), src)
        return super().rename_noreplace(src, dst)

    def unlink(self, p):
        if self.crash_on_unlink_of and p == self.crash_on_unlink_of:
            raise SimulatedCrash()
        return super().unlink(p)


def shm_dir():
    base = "/dev/shm"
    if not os.path.isdir(base) or not os.access(base, os.W_OK):
        pytest.skip("no tmpfs at /dev/shm for a real cross-volume test")
    d = os.path.join(base, "curator-test-" + uuid.uuid4().hex[:8])
    os.makedirs(d)
    return d


@pytest.fixture
def other_volume():
    d = shm_dir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def music_lib(lib, n=3, big=False):
    for i in range(1, n + 1):
        p = make_audio(str(lib / f"in/t{i}.flac"), title=f"Song {i}", artist="Band", album="Rec",
                       albumartist="Band", tracknumber=str(i))
        if big:
            with open(p, "ab") as fh:
                fh.write(os.urandom(3 * 1024 * 1024))
    return lib


def run(engine, p, **kw):
    engine.scan(p)
    store = engine.plan(p)
    return store, engine.apply(store, **kw)


def contents(root):
    out = {}
    for dp, _, fns in os.walk(root):
        for f in fns:
            with open(os.path.join(dp, f), "rb") as fh:
                out[os.path.relpath(os.path.join(dp, f), root)] = fh.read()
    return out


# ------------------------------------------------------------------ basic apply

def test_apply_moves_and_is_idempotent(engine, lib):
    music_lib(lib)
    before = contents(lib)
    p = profile([lib], music={})
    store, res = run(engine, p)
    assert res.status == "completed" and res.done == 3
    after = contents(lib)
    assert sorted(after) == ["Band/Rec/01 - Song 1.flac", "Band/Rec/02 - Song 2.flac", "Band/Rec/03 - Song 3.flac"]
    assert sorted(after.values()) == sorted(before.values())
    assert not (lib / "in").exists() or list((lib / "in").iterdir()) == []
    # repeating produces no changes
    engine.scan(p)
    again = engine.plan(p)
    assert again.count("changes") == 0 and again.count("unchanged") == 3


def test_existing_destination_never_overwritten(engine, lib, tmp_path):
    music_lib(lib, 1)
    dest = tmp_path / "M"
    make_file(str(dest / "Band/Rec/01 - Song 1.flac"), b"someone else's file")
    p = profile([lib], music={"root": str(dest)})
    store, res = run(engine, p)
    it = next(store.items())
    assert it.action == "review" and "conflict" in it.flags
    assert (dest / "Band/Rec/01 - Song 1.flac").read_bytes() == b"someone else's file"
    assert (lib / "in/t1.flac").exists()


def test_file_appearing_after_preview_is_not_overwritten(engine, lib, tmp_path):
    music_lib(lib, 1)
    dest = tmp_path / "M"
    p = profile([lib], music={"root": str(dest)})
    engine.scan(p)
    store = engine.plan(p)
    make_file(str(dest / "Band/Rec/01 - Song 1.flac"), b"raced in")
    res = engine.apply(store)
    assert res.done == 0 and res.skipped == 1
    assert (dest / "Band/Rec/01 - Song 1.flac").read_bytes() == b"raced in"
    assert (lib / "in/t1.flac").exists()


def test_alternate_name_policy_is_deterministic(engine, lib, tmp_path):
    make_audio(str(lib / "a/x.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1", salt="a")
    make_audio(str(lib / "b/x.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1", salt="b")
    p = profile([lib], music={"root": str(tmp_path / "M")}, conflict_policy="alternate")
    engine.scan(p)
    dests = sorted(rel(i.dest, tmp_path / "M") for i in engine.plan(p).items())
    assert dests == ["A/B/01 - T (2).flac", "A/B/01 - T.flac"]


def test_exact_duplicates_identified_by_hash(engine, lib, tmp_path):
    make_audio(str(lib / "a/x.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    shutil.copy2(lib / "a/x.flac", lib / "a/copy.flac")
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    items = {os.path.basename(i.source): i for i in engine.plan(p).items()}
    # deterministic winner: first by source path
    assert items["x.flac"].action == "skip" and "duplicate" in items["x.flac"].flags
    assert items["x.flac"].content_hash
    assert items["copy.flac"].action == "move"


# ------------------------------------------------------------------ rename cycles / case-only

def test_rename_cycle_swap(engine, lib):
    a = make_audio(str(lib / "02 - Two.flac"), title="One", artist="A", album="B", albumartist="A", tracknumber="1",
                   salt="one")
    b = make_audio(str(lib / "01 - One.flac"), title="Two", artist="A", album="B", albumartist="A", tracknumber="2",
                   salt="two")
    ca, cb = open(a, "rb").read(), open(b, "rb").read()
    p = profile([lib], music={"mode": "rename"})
    store, res = run(engine, p)
    assert res.done == 2, res.errors
    assert (lib / "01 - One.flac").read_bytes() == ca
    assert (lib / "02 - Two.flac").read_bytes() == cb
    assert not [f for f in os.listdir(lib) if f.startswith(".curator")]


def test_case_only_rename(engine, lib):
    f = make_audio(str(lib / "01 - one.flac"), title="One", artist="A", album="B", albumartist="A", tracknumber="1")
    data = open(f, "rb").read()
    p = profile([lib], music={"mode": "rename"})
    store, res = run(engine, p)
    assert res.done == 1, res.errors
    assert os.listdir(lib) == ["01 - One.flac"] and (lib / "01 - One.flac").read_bytes() == data


# ------------------------------------------------------------------ cross volume

def test_cross_volume_verified_copy(engine, lib, other_volume):
    music_lib(lib, 2, big=True)
    src_data = contents(lib / "in")
    mt = {n: os.stat(lib / "in" / n).st_mtime_ns for n in src_data}
    p = profile([lib], music={"root": other_volume})
    store, res = run(engine, p)
    assert os.stat(lib).st_dev != os.stat(other_volume).st_dev
    assert all(i.cross_device for i in store.query("action='move'"))
    assert res.status == "completed" and res.done == 2
    out = contents(other_volume)
    assert sorted(out.values()) == sorted(src_data.values())
    assert os.stat(os.path.join(other_volume, "Band/Rec/01 - Song 1.flac")).st_mtime_ns == mt["t1.flac"]
    assert not os.path.exists(lib / "in/t1.flac")
    assert not [f for dp, _, fs in os.walk(other_volume) for f in fs if f.startswith(".curator")]


def test_interrupted_copy_recovers_without_losing_source(engine, lib, other_volume):
    music_lib(lib, 1, big=True)
    original = (lib / "in/t1.flac").read_bytes()
    p = profile([lib], music={"root": other_volume})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.crash_after_bytes = 1024 * 1024 + 7
    with pytest.raises(SimulatedCrash):
        engine.apply(store, fs=fs)
    assert (lib / "in/t1.flac").read_bytes() == original
    partials = [f for dp, _, fns in os.walk(other_volume) for f in fns if f.startswith(".curator-part")]
    assert len(partials) == 1
    # restart: resume reuses the verified prefix and completes
    rep = engine.recover(resume=True)
    assert rep.results and rep.results[0].done == 1
    assert open(os.path.join(other_volume, "Band/Rec/01 - Song 1.flac"), "rb").read() == original
    assert not (lib / "in/t1.flac").exists()
    assert not [f for dp, _, fns in os.walk(other_volume) for f in fns if f.startswith(".curator")]


def test_interrupted_copy_recovery_without_resume_restores_clean_state(engine, lib, other_volume):
    music_lib(lib, 1, big=True)
    p = profile([lib], music={"root": other_volume})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.crash_after_bytes = 100
    with pytest.raises(SimulatedCrash):
        engine.apply(store, fs=fs)
    engine.recover(resume=False)
    assert (lib / "in/t1.flac").exists()
    assert not [f for dp, _, fns in os.walk(other_volume) for f in fns]


def test_crash_after_finalize_before_source_removal(engine, lib, other_volume):
    music_lib(lib, 1)
    original = (lib / "in/t1.flac").read_bytes()
    p = profile([lib], music={"root": other_volume})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.crash_on_unlink_of = str(lib / "in/t1.flac")
    with pytest.raises(SimulatedCrash):
        engine.apply(store, fs=fs)
    assert (lib / "in/t1.flac").exists()  # source still there
    rep = engine.recover()
    assert any("Removed original after verified copy" in a for a in rep.actions)
    assert open(os.path.join(other_volume, "Band/Rec/01 - Song 1.flac"), "rb").read() == original
    assert not (lib / "in/t1.flac").exists()


def test_disk_full_keeps_source(engine, lib, other_volume):
    music_lib(lib, 2)
    p = profile([lib], music={"root": other_volume})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.write_errno = errno.ENOSPC
    res = engine.apply(store, fs=fs)
    assert res.status == "failed" and "full" in res.message
    assert (lib / "in/t1.flac").exists() and (lib / "in/t2.flac").exists()
    assert not [f for dp, _, fns in os.walk(other_volume) for f in fns]


def test_source_modified_during_copy_is_kept(engine, lib, other_volume):
    music_lib(lib, 1, big=True)
    p = profile([lib], music={"root": other_volume})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    target = lib / "in/t1.flac"

    def touch():
        if fs.on_write:
            fs.on_write = None
            with open(target, "ab") as fh:
                fh.write(b"edited by another program")
    fs.on_write = touch
    res = engine.apply(store, fs=fs)
    assert res.done == 0 and res.failed == 1
    assert target.read_bytes().endswith(b"edited by another program")
    assert not os.path.exists(os.path.join(other_volume, "Band/Rec/01 - Song 1.flac"))


# ------------------------------------------------------------------ concurrent change / transient / unavailable

def test_change_after_scan_blocks_item(engine, lib):
    music_lib(lib, 2)
    p = profile([lib], music={})
    engine.scan(p)
    store = engine.plan(p)
    with open(lib / "in/t1.flac", "ab") as fh:
        fh.write(b"changed")
    res = engine.apply(store)
    assert res.done == 1 and res.skipped == 1
    assert (lib / "in/t1.flac").exists()
    assert any("changed since it was scanned" in why for _, why in res.preflight)


def test_transient_errors_are_retried_with_backoff(engine, lib):
    music_lib(lib, 1)
    p = profile([lib], music={})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.rename_errnos = [errno.EBUSY, errno.EAGAIN]
    sleeps = []
    res = engine.apply(store, fs=fs, sleep=sleeps.append)
    assert res.done == 1 and sleeps == [0.5, 1.0]


def test_unavailable_share_interrupts_then_resumes(engine, lib):
    music_lib(lib, 2)
    p = profile([lib], music={})
    engine.scan(p)
    store = engine.plan(p)
    fs = FaultFS()
    fs.rename_errnos = [errno.ENOTCONN] * 10
    res = engine.apply(store, fs=fs, sleep=lambda s: None)
    assert res.status == "interrupted" and "Reconnect" in res.message
    assert (lib / "in/t1.flac").exists() and (lib / "in/t2.flac").exists()
    rep = engine.recover(resume=True)
    assert rep.results[0].done == 2
    assert sorted(contents(lib)) == ["Band/Rec/01 - Song 1.flac", "Band/Rec/02 - Song 2.flac"]


def test_reconnected_path_pointing_at_different_storage_is_refused(engine, lib, tmp_path):
    music_lib(lib, 1)
    p = profile([lib], music={})
    engine.scan(p)
    store = engine.plan(p)
    os.rename(lib, tmp_path / "moved-away")
    lib.mkdir()
    shutil.copytree(tmp_path / "moved-away/in", lib / "in")
    with pytest.raises(PreflightError) as e:
        engine.apply(store)
    assert "not the same folder" in str(e.value)


def test_generated_path_cannot_escape_destination(engine, lib, tmp_path):
    music_lib(lib, 1)
    p = profile([lib], music={"root": str(tmp_path / "M")})
    engine.scan(p)
    store = engine.plan(p)
    it = next(store.query("action='move'"))
    it.dest = str(tmp_path / "elsewhere/evil.flac")
    store.update(it)
    res = engine.apply(store)
    assert res.done == 0 and "outside the selected destination" in res.preflight[0][1]
    assert not (tmp_path / "elsewhere").exists()


def test_destination_symlink_escape_blocked(engine, lib, tmp_path):
    music_lib(lib, 1)
    dest = tmp_path / "M"
    (dest).mkdir()
    (tmp_path / "outside").mkdir()
    os.symlink(tmp_path / "outside", dest / "Band")
    p = profile([lib], music={"root": str(dest)})
    store, res = run(engine, p)
    assert res.done == 0
    assert not list((tmp_path / "outside").iterdir())


# ------------------------------------------------------------------ cancellation

def test_cancel_between_items(engine, lib):
    music_lib(lib, 3)
    p = profile([lib], music={})
    engine.scan(p)
    store = engine.plan(p)
    from datacurator.engine.executor import Control
    c = Control()
    seen = []

    def prog(ev):
        seen.append(ev)
        if len(seen) == 2:
            c.cancel()
    res = engine.apply(store, control=c, progress=prog)
    assert res.status == "cancelled" and res.done == 1
    files = contents(lib)
    assert len(files) == 3  # nothing lost or half-moved


# ------------------------------------------------------------------ undo

def test_undo_restores_original_layout(engine, lib, other_volume):
    music_lib(lib, 2)
    make_file(str(lib / "in/cover.jpg"), b"jpg")
    before = snapshot(str(lib))
    before_files = {k: v[2] for k, v in before.items() if not k.endswith("/")}
    p = profile([lib], music={"root": other_volume})
    store, res = run(engine, p)
    assert res.done == 3
    rep = engine.undo(res.batch_id)
    assert rep.complete and rep.restored == 3, rep.summary()
    after = snapshot(str(lib))
    assert {k: v[2] for k, v in after.items() if not k.endswith("/")} == before_files
    assert os.listdir(other_volume) == []  # created folders removed again


def test_undo_conflicts_are_reported_not_forced(engine, lib):
    music_lib(lib, 3)
    p = profile([lib], music={})
    store, res = run(engine, p)
    with open(lib / "Band/Rec/01 - Song 1.flac", "ab") as fh:
        fh.write(b"user edited")
    make_file(str(lib / "in/t2.flac"), b"new file at original place")
    rep = engine.undo(res.batch_id)
    assert not rep.complete and rep.restored == 1
    assert len(rep.conflicts) == 2
    assert (lib / "in/t3.flac").exists()
    assert (lib / "Band/Rec/01 - Song 1.flac").read_bytes().endswith(b"user edited")
    assert (lib / "in/t2.flac").read_bytes() == b"new file at original place"
    assert engine.journal.batch(res.batch_id)["status"] == "partially_undone"


def test_cleanup_empty_dirs_and_undo_recreates(engine, lib):
    music_lib(lib, 1)
    make_file(str(lib / "keep/notes.docx"), b"x")
    make_audio(str(lib / "keep/a.flac"), title="K", artist="Z", album="Y", albumartist="Z", tracknumber="1")
    p = profile([lib], music={}, cleanup_empty_dirs=True)
    store, res = run(engine, p)
    assert not (lib / "in").exists()
    assert (lib / "keep/notes.docx").exists()  # non-empty folder never removed
    engine.undo(res.batch_id)
    assert (lib / "in/t1.flac").exists()


def test_missing_destination_root_and_parents_are_created_and_undone(engine, lib, tmp_path):
    music_lib(lib, 1)
    dest = tmp_path / "new/deeper/Music"
    p = profile([lib], music={"root": str(dest)})
    store, res = run(engine, p)
    assert res.done == 1, res.errors
    assert (dest / "Band/Rec/01 - Song 1.flac").exists()
    engine.undo(res.batch_id)
    assert not (tmp_path / "new").exists() and (lib / "in/t1.flac").exists()
