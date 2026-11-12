"""Tests for the interface-agnostic runtime: errors, shutdown, progress, traversal, batches."""
import errno
import io
import json
import os
import signal
import sqlite3
import sys
import threading

import pytest

from datacurator.core.errors import ActionableError, ExitCode, explain_exception
from datacurator.core.lifecycle import ShutdownCoordinator
from datacurator.core.progress import (JsonLinesProgress, ProgressEvent, TerminalProgress,
                                       adapt_legacy_callback, format_bytes, format_duration)
from datacurator.engine.batch import is_transient, run_resilient
from datacurator.models import CuratorError, PreflightError
from datacurator.storage.walk import WalkCancelled, WalkReport, iter_files, walk


# ------------------------------------------------------------------ errors
def test_errors_explain_common_failures():
    e = explain_exception(PermissionError(errno.EACCES, "Permission denied", "/x/y"))
    assert "Permission denied: /x/y" in e.summary and e.fix
    e = explain_exception(OSError(errno.ENOSPC, "No space left on device"))
    assert "full" in e.summary and "recover --resume" in e.fix
    e = explain_exception(json.JSONDecodeError("Expecting value", "{", 1))
    assert "JSON" in e.summary and "init-profile" in e.fix
    e = explain_exception(sqlite3.OperationalError("database is locked"))
    assert "in use" in e.summary
    e = explain_exception(RuntimeError("boom"))
    assert e.summary == "Unexpected internal error." and "boom" in e.why


def test_errors_keep_plain_messages_and_exit_codes():
    assert explain_exception(FileNotFoundError("No plan abc")).summary == "No plan abc"
    assert explain_exception(CuratorError("nope")).summary == "nope"
    assert explain_exception(PreflightError("blocked")).exit_code == ExitCode.PREFLIGHT
    assert explain_exception(InterruptedError("Planning cancelled")).exit_code == ExitCode.INTERRUPTED
    a = ActionableError("x", why="w", fix="f", details="d")
    assert explain_exception(a) is a
    assert a.render() == "error: x\n  why: w\n  fix: f"
    assert a.render(verbose=True).endswith("details: d")


# ---------------------------------------------------------------- lifecycle
@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="needs main thread")
def test_first_signal_is_graceful_second_forces():
    seen = []
    prev = signal.getsignal(signal.SIGTERM)
    with ShutdownCoordinator(announce=seen.append) as sd:
        called = []
        sd.attach(lambda: called.append(1))
        signal.raise_signal(signal.SIGTERM)
        assert sd.requested and sd.event.is_set() and called == [1] and len(seen) == 1
        assert sd.exit_code == ExitCode.TERMINATED
        with pytest.raises(SystemExit) as x:
            signal.raise_signal(signal.SIGTERM)
        assert x.value.code == ExitCode.TERMINATED
    assert signal.getsignal(signal.SIGTERM) == prev          # handlers restored


def test_sigint_second_press_raises_keyboard_interrupt():
    with ShutdownCoordinator() as sd:
        signal.raise_signal(signal.SIGINT)
        assert sd.requested and sd.exit_code == ExitCode.INTERRUPTED
        with pytest.raises(KeyboardInterrupt):
            signal.raise_signal(signal.SIGINT)


def test_attach_after_request_runs_immediately_and_detach_works():
    sd = ShutdownCoordinator()
    sd.request("test")
    hit = []
    sd.attach(lambda: hit.append(1))
    assert hit == [1]
    detach = sd.attach(lambda: hit.append(2))
    detach()
    sd.request("again")           # not the first request: callbacks do not rerun
    assert hit == [1, 2]


def test_coordinator_is_inert_off_main_thread():
    out = {}

    def work():
        with ShutdownCoordinator() as sd:
            out["ok"] = not sd.requested
    t = threading.Thread(target=work)
    t.start()
    t.join()
    assert out == {"ok": True}


# ----------------------------------------------------------------- progress
def test_format_helpers():
    assert format_bytes(0) == "0 B" and format_bytes(1536) == "1.5 KB" and format_bytes(5 * 1024 ** 3) == "5.0 GB"
    assert format_duration(5) == "0:05" and format_duration(3725) == "1:02:05"


def test_adapter_handles_both_legacy_shapes():
    events = []

    class Sink:
        def update(self, ev):
            events.append(ev)

        def close(self, summary=None):
            pass
    cb = adapt_legacy_callback(Sink(), "scan")
    cb("/music/a", 12)
    cb({"done": 3, "total": 10, "current": "x.flac", "bytes": 2048})
    assert events[0] == ProgressEvent("scan", 12, None, 0, "/music/a")
    assert events[1] == ProgressEvent("scan", 3, 10, 2048, "x.flac")


def test_terminal_progress_plain_mode_is_sparse_and_tty_free():
    buf = io.StringIO()
    p = TerminalProgress(stream=buf, plain_interval=0.0)
    p.update(ProgressEvent("apply", 5, 10, 1024, "a.mkv"))
    p.close("Done.")
    out = buf.getvalue()
    assert "\x1b" not in out and "5/10 (50.0%)" in out and "1.0 KB" in out and out.endswith("Done.\n")


def test_jsonl_progress_is_machine_readable():
    buf = io.StringIO()
    p = JsonLinesProgress(stream=buf, min_interval=0)
    p.update(ProgressEvent("scan", 7, None, 0, "/x"))
    p.close("ok")
    lines = [json.loads(x) for x in buf.getvalue().splitlines()]
    assert lines[0]["type"] == "progress" and lines[0]["done"] == 7 and lines[1]["type"] == "summary"


# --------------------------------------------------------------------- walk
def _tree(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "2.txt").write_text("22")
    (tmp_path / "a" / "1.txt").write_text("1")
    (tmp_path / "b" / "z.txt").write_text("zzz")
    (tmp_path / ".hidden").write_text("h")
    (tmp_path / "top.txt").write_text("t")


def test_walk_is_deterministic_and_skips_hidden(tmp_path):
    _tree(tmp_path)
    names = [os.path.relpath(f.path, tmp_path) for f in iter_files([str(tmp_path)])]
    assert names == ["top.txt", os.path.join("a", "1.txt"), os.path.join("a", "2.txt"), os.path.join("b", "z.txt")]
    assert [f.size for f in iter_files([str(tmp_path)])][:2] == [1, 1]
    hidden = [f.name for f in iter_files([str(tmp_path)], include_hidden=True)]
    assert ".hidden" in hidden


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_walk_reports_symlinks_and_loops_without_failing(tmp_path):
    _tree(tmp_path)
    os.symlink(str(tmp_path), str(tmp_path / "a" / "loop"))
    rep = WalkReport()
    files = [f for ls in walk(str(tmp_path), report=rep) for f in ls.files]
    assert len(files) == 4 and rep.of_kind("symlink") and rep.complete
    rep2 = WalkReport()
    list(walk(str(tmp_path), follow_symlinks=True, report=rep2))
    assert rep2.of_kind("loop")                       # followed once, then recognised as a cycle


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX permissions and a non-root user")
def test_walk_records_unreadable_directory_and_marks_incomplete(tmp_path):
    _tree(tmp_path)
    locked = tmp_path / "b"
    locked.chmod(0)
    try:
        rep = WalkReport()
        files = [f.name for ls in walk(str(tmp_path), report=rep) for f in ls.files]
    finally:
        locked.chmod(0o755)
    assert "z.txt" not in files and "top.txt" in files
    assert rep.of_kind("denied") and not rep.complete


def test_walk_cancel_and_missing_root(tmp_path):
    _tree(tmp_path)
    ev = threading.Event()
    ev.set()
    with pytest.raises(WalkCancelled):
        list(walk(str(tmp_path), cancel=ev))
    rep = WalkReport()
    assert list(walk(str(tmp_path / "nope"), report=rep)) == [] and rep.of_kind("vanished") and not rep.complete


def test_walk_prune_skips_directories(tmp_path):
    _tree(tmp_path)
    names = [f.name for f in iter_files([str(tmp_path)], prune=lambda d: "skip" if d.endswith("a") else None)]
    assert "1.txt" not in names and "z.txt" in names


# -------------------------------------------------------------------- batch
def test_batch_isolates_failures_and_keeps_order():
    def fn(x):
        if x == 2:
            raise ValueError("bad item")
        return x * 10
    rep = run_resilient(range(4), fn)
    assert [o.value for o in rep.succeeded] == [0, 10, 30]
    assert len(rep.failed) == 1 and rep.failed[0].key == "2" and not rep.clean


def test_batch_retries_transient_then_succeeds():
    calls = {"n": 0}

    def flaky(_):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(errno.ETIMEDOUT, "timed out")
        return "ok"
    rep = run_resilient(["x"], flaky, retries=3, base_delay=0.001, max_delay=0.002)
    assert rep.clean and rep.outcomes[0].attempts == 3


def test_batch_does_not_retry_permanent_errors():
    calls = {"n": 0}

    def fn(_):
        calls["n"] += 1
        raise PermissionError(errno.EACCES, "no")
    rep = run_resilient(["x"], fn, retries=5, base_delay=0.001)
    assert calls["n"] == 1 and rep.failed[0].error.fix


def test_batch_disk_full_aborts_remaining_items():
    def fn(x):
        if x == 1:
            raise OSError(errno.ENOSPC, "No space left on device")
        return x
    rep = run_resilient(range(10), fn)
    assert rep.aborted and "full" in rep.aborted and rep.pending == 8
    assert len(rep.outcomes) == 2


def test_batch_cancel_stops_new_work_and_interrupts_backoff():
    ev = threading.Event()
    seen = []

    def fn(x):
        seen.append(x)
        if x == 1:
            ev.set()
        return x
    rep = run_resilient(range(10), fn, cancel=ev)
    assert rep.cancelled and seen == [0, 1] and rep.pending == 8


def test_batch_backoff_sleep_is_interruptible():
    import time
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()

    def always_transient(_):
        raise OSError(errno.ETIMEDOUT, "timed out")
    t0 = time.monotonic()
    rep = run_resilient(["a"], always_transient, retries=5, base_delay=30, max_delay=30, cancel=ev)
    assert time.monotonic() - t0 < 5 and rep.cancelled and "Cancelled" in rep.failed[0].error.summary


def test_batch_parallel_bounded_and_complete():
    lock = threading.Lock()
    active = {"now": 0, "max": 0}

    def fn(x):
        with lock:
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        try:
            return x
        finally:
            with lock:
                active["now"] -= 1
    outcomes = []
    rep = run_resilient(iter(range(200)), fn, workers=4, on_outcome=outcomes.append)
    assert len(rep.succeeded) == 200 and len(outcomes) == 200 and active["max"] <= 4
    assert sorted(o.value for o in rep.succeeded) == list(range(200))


def test_is_transient_classification():
    assert is_transient(OSError(errno.ETIMEDOUT, "x")) and not is_transient(PermissionError(errno.EACCES, "x"))
    assert not is_transient(ValueError("x"))
