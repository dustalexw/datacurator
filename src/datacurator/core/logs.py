"""Structured logging: quiet console by default, rotating machine-readable file always.

Console: WARNING (default), INFO with ``-v``, DEBUG with ``-vv`` (stderr, human format).
File:    DEBUG, JSON lines, ``<log dir>/datacurator.log`` (5 x 2 MB rotation). The path is
         platform-appropriate (platformdirs) and can be overridden with DATACURATOR_LOG_DIR.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time

ROOT = "datacurator"
_MARK = "_datacurator_handler"


class JsonFormatter(logging.Formatter):
    _SKIP = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        doc = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)) + f".{int(record.msecs):03d}",
               "level": record.levelname, "logger": record.name, "msg": record.getMessage()}
        doc.update({k: v for k, v in record.__dict__.items() if k not in self._SKIP and not k.startswith("_")})
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False, default=str)


def default_log_dir() -> str:
    env = os.environ.get("DATACURATOR_LOG_DIR")
    if env:
        return env
    try:
        from platformdirs import user_log_dir
        return user_log_dir("DataCurator", "DataCurator")
    except Exception:
        return os.path.join(os.path.expanduser("~"), ".datacurator", "logs")


def configure_logging(verbosity: int = 0, log_dir: str | None = None, stream=None) -> str | None:
    """Install handlers once (idempotent). Returns the log file path, or None if it is not writable."""
    log = logging.getLogger(ROOT)
    for h in list(log.handlers):
        if getattr(h, _MARK, False):
            log.removeHandler(h)
            h.close()
    log.setLevel(logging.DEBUG)
    log.propagate = False

    console = logging.StreamHandler(stream or sys.stderr)
    console.setLevel(logging.WARNING if verbosity <= 0 else logging.INFO if verbosity == 1 else logging.DEBUG)
    console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    setattr(console, _MARK, True)
    log.addHandler(console)

    path: str | None = None
    try:
        d = log_dir or default_log_dir()
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, "datacurator.log")
        fh = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(JsonFormatter())
        setattr(fh, _MARK, True)
        log.addHandler(fh)
    except OSError:
        path = None  # logging must never stop the tool from working
    return path


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{ROOT}.{name}" if not name.startswith(ROOT) else name)
