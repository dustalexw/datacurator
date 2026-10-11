"""Background work for the GUI. The engine runs off the UI thread; results come back via signals."""
from __future__ import annotations

import threading
import traceback

from PySide6.QtCore import QObject, QThread, Signal

from ..engine.executor import Control
from ..engine.music import library_rows


class Worker(QObject):
    progress = Signal(str, int, int)      # message, done, total (total 0 = indeterminate)
    finished = Signal(object)             # result
    failed = Signal(str)

    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kw = kw
        self.cancel_event = threading.Event()
        self.control = Control()

    def run(self):
        try:
            res = self.fn(self, *self.args, **self.kw)
            self.finished.emit(res)
        except Exception as e:  # surface every error to the UI in plain words
            msg = str(e) or e.__class__.__name__
            if not getattr(e, "plain", False) and not isinstance(e, (ValueError, OSError)) \
                    and e.__class__.__module__ != "datacurator.models":
                msg += "\n\n" + traceback.format_exc(limit=4)
            self.failed.emit(msg)

    def cancel(self):
        self.cancel_event.set()
        self.control.cancel()


def start(parent, worker: Worker) -> QThread:
    th = QThread(parent)
    worker.moveToThread(th)
    th.started.connect(worker.run)
    worker.finished.connect(th.quit)
    worker.failed.connect(th.quit)
    th.finished.connect(th.deleteLater)
    th.start()
    return th


# ---------------------------------------------------------------- jobs

def job_scan_plan(w: Worker, engine, profile):
    def sp(d, n):
        w.progress.emit(f"Scanning… {n:,} files", n, 0)
    stats = engine.scan(profile, progress=sp, cancel=w.cancel_event)
    if stats.cancelled:
        return {"cancelled": True, "stats": stats}

    def pp(d, n):
        w.progress.emit(f"Planning… {n:,} files", n, 0)
    store = engine.plan(profile, progress=pp, cancel=w.cancel_event)
    return {"stats": stats, "store": store,
            "music": library_rows(engine.catalog, profile["sources"], w.cancel_event)}


def job_plan(w: Worker, engine, profile):
    def pp(d, n):
        w.progress.emit(f"Planning… {n:,} files", n, 0)
    return {"store": engine.plan(profile, progress=pp, cancel=w.cancel_event),
            "music": library_rows(engine.catalog, profile["sources"], w.cancel_event)}


def job_music_library(w: Worker, engine, profile, scan=False):
    if scan:
        stats = engine.scan(profile, progress=lambda d, n: w.progress.emit(f"Scanning… {n:,} files", n, 0),
                            cancel=w.cancel_event)
        if stats.cancelled:
            return None
    return library_rows(engine.catalog, profile["sources"], w.cancel_event)


def job_apply(w: Worker, engine, store, approve_deletes=False):
    def cb(ev):
        w.progress.emit(f"Applying… {ev['done']:,} of {ev['total']:,}", ev["done"], ev["total"])
    return engine.apply(store, progress=cb, control=w.control, approve_deletes=approve_deletes)


def job_undo(w: Worker, engine, batch_id, dry_run=False):
    w.progress.emit("Undoing…", 0, 0)
    return engine.undo(batch_id, dry_run=dry_run)


def job_recover(w: Worker, engine, resume=False):
    w.progress.emit("Checking interrupted work…", 0, 0)
    return engine.recover(resume=resume)


def job_probe(w: Worker, path):
    from ..storage.capabilities import probe
    return probe(path)
