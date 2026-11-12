"""Typed progress events and renderers.

The engine historically reports progress in two incompatible shapes: ``(folder, file_count)``
from scan/plan and a ``dict`` from apply. `adapt_legacy_callback` normalizes both into
`ProgressEvent`, so every renderer (terminal, JSON lines, GUI) consumes one type.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol, TextIO


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    phase: str                       # "scan" | "plan" | "apply" | "undo" | "dedupe"
    done: int = 0
    total: int | None = None         # None = not known yet (scan)
    bytes_done: int = 0
    current: str = ""


class ProgressSink(Protocol):
    def update(self, ev: ProgressEvent) -> None: ...
    def close(self, summary: str | None = None) -> None: ...


class NullProgress:
    def update(self, ev: ProgressEvent) -> None:
        pass

    def close(self, summary: str | None = None) -> None:
        pass


def format_bytes(n: float) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(f) < 1024 or unit == "TB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.1f} {unit}"
        f /= 1024
    return str(n)  # pragma: no cover


def format_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _shorten_middle(text: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(text) <= width:
        return text
    keep = width - 1
    head = keep // 2
    return text[:head] + "…" + text[len(text) - (keep - head):]


class TerminalProgress:
    """Single-line live status on stderr; falls back to sparse plain lines when not a TTY.

    Shows files, bytes, throughput and ETA (exponentially smoothed so it does not jitter).
    Output goes to stderr so stdout stays clean for pipes and `--json` consumers.
    """

    def __init__(self, stream: TextIO | None = None, min_interval: float = 0.1, plain_interval: float = 10.0):
        self.stream = stream or sys.stderr
        self.tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self.interval = min_interval if self.tty else plain_interval
        self._last_draw = 0.0
        self._start = time.monotonic()
        self._rate: float | None = None
        self._prev: tuple[float, int] | None = None
        self._dirty = False

    def update(self, ev: ProgressEvent) -> None:
        now = time.monotonic()
        # smooth items/sec with an EMA over >=0.5 s windows
        if self._prev is None:
            self._prev = (now, ev.done)
        elif now - self._prev[0] >= 0.5:
            inst = (ev.done - self._prev[1]) / (now - self._prev[0])
            self._rate = inst if self._rate is None else 0.7 * self._rate + 0.3 * inst
            self._prev = (now, ev.done)
        if now - self._last_draw < self.interval:
            return
        self._last_draw = now
        line = self._format(ev, now)
        if self.tty:
            width = shutil.get_terminal_size((100, 20)).columns
            self.stream.write("\r\x1b[2K" + _shorten_middle(line, width - 1))
            self._dirty = True
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

    def _format(self, ev: ProgressEvent, now: float) -> str:
        head = f"{ev.phase:<6}"
        if ev.total:
            pct = 100 * ev.done / ev.total
            body = f"{ev.done}/{ev.total} ({pct:4.1f}%)"
        else:
            body = f"{ev.done} files"
        extras = []
        if ev.bytes_done:
            extras.append(format_bytes(ev.bytes_done))
        if ev.total and self._rate and self._rate > 0 and ev.done < ev.total:
            extras.append(f"ETA {format_duration((ev.total - ev.done) / self._rate)}")
        extras.append(f"{format_duration(now - self._start)} elapsed")
        cur = f"  {ev.current}" if ev.current else ""
        return f"{head} {body}  " + " · ".join(extras) + cur

    def close(self, summary: str | None = None) -> None:
        if self.tty and self._dirty:
            self.stream.write("\r\x1b[2K")
        if summary:
            self.stream.write(summary + "\n")
        self.stream.flush()
        self._dirty = False


class JsonLinesProgress:
    """One JSON object per line (`{"type": "progress", ...}`) for wrappers and CI."""

    def __init__(self, stream: TextIO | None = None, min_interval: float = 0.25):
        self.stream = stream or sys.stderr
        self.interval = min_interval
        self._last = 0.0

    def update(self, ev: ProgressEvent) -> None:
        now = time.monotonic()
        if now - self._last < self.interval:
            return
        self._last = now
        self.stream.write(json.dumps({"type": "progress", **asdict(ev)}, ensure_ascii=False) + "\n")
        self.stream.flush()

    def close(self, summary: str | None = None) -> None:
        if summary:
            self.stream.write(json.dumps({"type": "summary", "message": summary}, ensure_ascii=False) + "\n")
        self.stream.flush()


def adapt_legacy_callback(sink: ProgressSink, phase: str) -> Callable[..., None]:
    """Return a callback accepting both legacy shapes: ``(folder, count)`` and ``({...})``."""

    def cb(*args: Any) -> None:
        if len(args) == 1 and isinstance(args[0], dict):
            d = args[0]
            sink.update(ProgressEvent(phase, int(d.get("done", 0)), d.get("total"), int(d.get("bytes", 0)),
                                      str(d.get("current", ""))))
        elif len(args) == 2:
            sink.update(ProgressEvent(phase, int(args[1]), None, 0, str(args[0])))
    return cb
