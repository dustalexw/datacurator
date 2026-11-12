"""Resilient batch execution: bounded concurrency, retry with backoff, cooperative cancel.

Use this for operations that are *independent per item* (hashing, metadata reads, network
lookups, thumbnails). Journaled file moves stay sequential on purpose: their ordering is
part of the crash-recovery contract.

Properties
----------
* One bad item never stops the batch: every item yields an `ItemOutcome`.
* Transient errors (network drop, busy file) are retried with exponential backoff + jitter;
  the backoff sleep is interruptible, so Ctrl-C is honoured within milliseconds.
* Fatal errors (disk full) abort the batch cleanly instead of failing every remaining item.
* Memory is bounded: at most ``workers * 2`` items are in flight, so a million-file
  iterable is never materialized.
"""
from __future__ import annotations

import errno
import random
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, TypeVar

from ..core.errors import ActionableError, explain_exception, os_error_is_disk_full

T = TypeVar("T")
R = TypeVar("R")

_TRANSIENT_ERRNOS = {errno.EAGAIN, errno.EBUSY, errno.ETIMEDOUT, errno.EINTR, errno.ECONNRESET,
                     errno.ECONNABORTED, errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EIO}


def is_transient(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    if getattr(exc, "winerror", None) in (32, 33, 53, 54, 59, 64, 67, 121, 1231):
        return True
    return exc.errno in _TRANSIENT_ERRNOS


@dataclass(slots=True)
class ItemOutcome(Generic[T, R]):
    item: T
    key: str
    ok: bool
    value: R | None = None
    error: ActionableError | None = None
    attempts: int = 1


@dataclass
class BatchReport(Generic[T, R]):
    outcomes: list[ItemOutcome[T, R]] = field(default_factory=list)
    cancelled: bool = False
    aborted: str | None = None           # reason a fatal error stopped the batch early
    pending: int = 0                     # items never started (cancel/abort)

    @property
    def succeeded(self) -> list[ItemOutcome[T, R]]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> list[ItemOutcome[T, R]]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def clean(self) -> bool:
        return not self.failed and not self.cancelled and self.aborted is None


def run_resilient(items: Iterable[T], fn: Callable[[T], R], *, key: Callable[[T], str] = str,
                  workers: int = 1, retries: int = 3, base_delay: float = 0.5, max_delay: float = 8.0,
                  is_transient_error: Callable[[BaseException], bool] = is_transient,
                  is_fatal_error: Callable[[BaseException], bool] | None = None,
                  cancel: threading.Event | None = None,
                  on_outcome: Callable[[ItemOutcome[T, R]], None] | None = None,
                  rng: random.Random | None = None) -> BatchReport[T, R]:
    """Apply `fn` to every item. See module docstring for guarantees.

    `workers=1` runs inline in input order (deterministic). With more workers outcomes are
    recorded in completion order. `on_outcome` is always called from the calling thread.
    """
    cancel = cancel or threading.Event()
    rng = rng or random.Random()
    fatal = is_fatal_error or (lambda e: isinstance(e, OSError) and os_error_is_disk_full(e))
    report: BatchReport[T, R] = BatchReport()
    abort = threading.Event()

    def attempt(item: T) -> ItemOutcome[T, R]:
        k = key(item)
        delay = base_delay
        n = 0
        while True:
            n += 1
            try:
                return ItemOutcome(item, k, True, value=fn(item), attempts=n)
            except Exception as e:  # noqa: BLE001 - boundary: converted to an outcome
                if fatal(e):
                    abort.set()
                    return ItemOutcome(item, k, False, error=explain_exception(e), attempts=n)
                if n > retries or not is_transient_error(e):
                    return ItemOutcome(item, k, False, error=explain_exception(e), attempts=n)
                if cancel.wait(min(delay, max_delay) * (0.75 + 0.5 * rng.random())):
                    return ItemOutcome(item, k, False, error=ActionableError("Cancelled while retrying."),
                                       attempts=n)
                delay *= 2

    def record(o: ItemOutcome[T, R]) -> None:
        report.outcomes.append(o)
        if on_outcome:
            on_outcome(o)

    def should_stop() -> bool:
        return cancel.is_set() or abort.is_set()

    it = iter(items)
    if workers <= 1:
        for item in it:
            if should_stop():
                report.pending += 1 + sum(1 for _ in it)
                break
            record(attempt(item))
    else:
        window = max(2, workers * 2)
        inflight: set[Future[ItemOutcome[T, R]]] = set()
        exhausted = False
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="curator-batch") as pool:
            while True:
                while not exhausted and not should_stop() and len(inflight) < window:
                    try:
                        inflight.add(pool.submit(attempt, next(it)))
                    except StopIteration:
                        exhausted = True
                if not inflight:
                    break
                done, inflight = wait(inflight, timeout=0.25, return_when=FIRST_COMPLETED)
                for f in done:
                    record(f.result())
                if should_stop() and not exhausted:
                    report.pending += sum(1 for _ in it)
                    exhausted = True
    report.cancelled = cancel.is_set()
    if abort.is_set():
        bad = next((o for o in reversed(report.outcomes) if o.error), None)
        report.aborted = bad.error.summary if bad and bad.error else "a fatal error occurred"
    return report
