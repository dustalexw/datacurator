"""Graceful interrupt handling (SIGINT / SIGTERM / SIGHUP / Windows SIGBREAK).

Policy
------
* First signal: request a *graceful* stop. A `threading.Event` is set and registered
  callbacks run (e.g. `Control.cancel`), so the engine finishes the current file, leaves
  the journal consistent and reports what was done.
* Second signal: the user insists. Raise immediately (`KeyboardInterrupt` / `SystemExit`)
  so a stuck network call cannot trap the process. The journal makes this safe: the next
  `curator recover` repairs whatever step was in flight.

Handlers can only be installed from the main thread; elsewhere (GUI worker threads,
tests under other runners) the coordinator degrades to a plain, never-set event.
"""
from __future__ import annotations

import signal
import threading
from types import FrameType, TracebackType
from typing import Callable

from .errors import ExitCode


def _signals() -> list[int]:
    names = ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    return [getattr(signal, n) for n in names if hasattr(signal, n)]


class ShutdownCoordinator:
    """Context manager translating termination signals into a cooperative cancel event."""

    def __init__(self, announce: Callable[[str], None] | None = None):
        self.event = threading.Event()
        self.signum: int | None = None
        self._count = 0
        self._callbacks: list[Callable[[], None]] = []
        self._previous: dict[int, object] = {}
        self._announce = announce or (lambda _msg: None)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ state
    @property
    def requested(self) -> bool:
        return self.event.is_set()

    @property
    def exit_code(self) -> int:
        return ExitCode.for_signal(self.signum) if self.signum is not None else int(ExitCode.INTERRUPTED)

    def attach(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Run `callback` on the first signal. Returns a function that detaches it."""
        with self._lock:
            self._callbacks.append(callback)
        if self.event.is_set():
            callback()

        def detach() -> None:
            with self._lock:
                if callback in self._callbacks:
                    self._callbacks.remove(callback)
        return detach

    def request(self, reason: str = "requested") -> None:
        """Programmatic graceful stop (what the first signal does)."""
        with self._lock:
            first = not self.event.is_set()
            self.event.set()
            callbacks = list(self._callbacks)
        if first:
            self._announce(f"Stopping after the current file ({reason}). Press Ctrl-C again to stop immediately.")
            for cb in callbacks:
                try:
                    cb()
                except Exception:  # a failing callback must not block shutdown
                    pass

    # --------------------------------------------------------------- handlers
    def _handle(self, signum: int, _frame: FrameType | None) -> None:
        self._count += 1
        self.signum = self.signum or signum
        if self._count == 1:
            self.request(signal.Signals(signum).name)
            return
        if signum == getattr(signal, "SIGINT", None):
            raise KeyboardInterrupt
        raise SystemExit(ExitCode.for_signal(signum))

    def __enter__(self) -> "ShutdownCoordinator":
        if threading.current_thread() is threading.main_thread():
            for s in _signals():
                try:
                    self._previous[s] = signal.signal(s, self._handle)
                except (OSError, ValueError):  # not settable on this platform/context
                    continue
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        for s, prev in self._previous.items():
            try:
                signal.signal(s, prev)  # type: ignore[arg-type]
            except (OSError, ValueError, TypeError):
                pass
        self._previous.clear()
