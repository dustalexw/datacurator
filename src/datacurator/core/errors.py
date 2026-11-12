"""Human-readable, recoverable errors and stable process exit codes.

Every failure a user can see should answer two questions: *why did it fail* and
*what exactly can I do about it*. `ActionableError` carries both; `explain_exception`
turns the raw exceptions that escape from the OS, SQLite or JSON parsing into one.
"""
from __future__ import annotations

import errno
import json
import sqlite3
import sys
from enum import IntEnum

from ..models import CuratorError


class ExitCode(IntEnum):
    """Process exit codes. Values 0-4 and 130 match the pre-existing CLI behaviour."""

    OK = 0
    ERROR = 1            # operation failed; nothing half-done
    PARTIAL = 2          # finished with per-file errors / undo incomplete
    NEEDS_RECOVERY = 3   # an earlier batch was interrupted: run `curator recover`
    PREFLIGHT = 4        # pre-checks refused to start; nothing was touched
    INTERRUPTED = 130    # SIGINT (128 + 2)
    TERMINATED = 143     # SIGTERM (128 + 15)

    @classmethod
    def for_signal(cls, signum: int) -> int:
        return 128 + int(signum)


class ActionableError(CuratorError):
    """An error that explains itself.

    `summary` is one line; `why` is the cause in plain words; `fix` is the exact action
    (preferably a command) that resolves it; `details` is technical text shown with -v.
    """

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(self, summary: str, *, why: str | None = None, fix: str | None = None,
                 details: str | None = None, exit_code: ExitCode | None = None):
        super().__init__(summary)
        self.summary = summary
        self.why = why
        self.fix = fix
        self.details = details
        if exit_code is not None:
            self.exit_code = exit_code

    def render(self, verbose: bool = False) -> str:
        lines = [f"error: {self.summary}"]
        if self.why:
            lines.append(f"  why: {self.why}")
        if self.fix:
            lines.append(f"  fix: {self.fix}")
        if verbose and self.details:
            lines.append(f"  details: {self.details}")
        return "\n".join(lines)


def _macos_privacy_hint(path: str | None) -> str:
    base = ("Grant your terminal (or the app) access in System Settings ▸ Privacy & Security ▸ "
            "Files and Folders / Full Disk Access, then run the command again.")
    return base if sys.platform == "darwin" else (
        f"Check ownership and permissions with `ls -ld {path or '<path>'}`, or run as a user that has access.")


def explain_exception(exc: BaseException) -> ActionableError:
    """Map any exception to an `ActionableError`. Already-actionable errors pass through;
    other `CuratorError`s keep their own (already plain-language) message."""
    if isinstance(exc, ActionableError):
        return exc
    detail = f"{exc.__class__.__name__}: {exc}"
    if isinstance(exc, CuratorError):
        code = ExitCode.PREFLIGHT if exc.__class__.__name__ == "PreflightError" else ExitCode.ERROR
        return ActionableError(str(exc), details=detail, exit_code=code)
    if isinstance(exc, json.JSONDecodeError):
        return ActionableError(
            "The profile file is not valid JSON.",
            why=f"line {exc.lineno}, column {exc.colno}: {exc.msg}",
            fix="Fix the syntax, or write a fresh starter with `curator init-profile my.json -s <folder>`.",
            details=detail)
    if isinstance(exc, sqlite3.DatabaseError):
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return ActionableError(
                "The Data Curator database is in use.",
                why="Another Data Curator process (the app or another `curator` command) holds it.",
                fix="Close the other instance and run the command again.", details=detail)
        if "malformed" in msg or "not a database" in msg or "corrupt" in msg:
            return ActionableError(
                "The Data Curator database is damaged.",
                why="The file was truncated or overwritten (for example by a crash or a sync tool).",
                fix="Move catalog.sqlite out of the data folder (run `curator --help` to see --data-dir); "
                    "it is rebuilt by the next scan. The journal (journal.sqlite) is separate: keep it.",
                details=detail)
        return ActionableError("The Data Curator database reported an error.", details=detail)
    if isinstance(exc, (PermissionError, FileNotFoundError)):
        path = getattr(exc, "filename", None)
        # Library code often raises FileNotFoundError("No plan abc") with only a message: keep it.
        bare = exc.args[0] if (not path and exc.errno is None and exc.args and isinstance(exc.args[0], str)) else None
        if isinstance(exc, PermissionError):
            return ActionableError(bare or f"Permission denied{f': {path}' if path else ''}.",
                                   why="The operating system refused access to this location.",
                                   fix=_macos_privacy_hint(path), details=detail)
        if bare:  # a library-supplied message such as "No plan abc" is already specific
            return ActionableError(bare, details=detail)
        return ActionableError(f"Not found{f': {path}' if path else ''}.",
                               why="The file or folder does not exist, or its drive/share is not connected.",
                               fix="Check the path for typos; for a removable drive or network share, reconnect it first.",
                               details=detail)
    # InterruptedError subclasses OSError: it must be tested first.
    if isinstance(exc, InterruptedError):
        return ActionableError("Cancelled.", details=detail, exit_code=ExitCode.INTERRUPTED)
    if isinstance(exc, OSError):
        e = exc.errno
        path = getattr(exc, "filename", None)
        where = f" ({path})" if path else ""
        table = {
            errno.ENOSPC: ("The destination is full.", "There is not enough free space.",
                           "Free up space or choose another destination, then `curator recover --resume`."),
            errno.EROFS: ("The destination is read-only.", "The volume is mounted read-only.",
                          "Remount it read-write or choose another destination."),
            errno.ENAMETOOLONG: ("A name is too long for this filesystem.",
                                 "The destination filesystem limits names or paths.",
                                 "Shorten the template (e.g. drop {episode_title}) and preview again."),
            errno.ENOTCONN: ("The network location disconnected.", "The share or drive went away.",
                             "Reconnect it (`curator locations connect <name>`), then `curator recover --resume`."),
            errno.ESTALE: ("The network location disconnected.", "The share or drive went away.",
                           "Reconnect it (`curator locations connect <name>`), then `curator recover --resume`."),
            errno.EBUSY: ("A file is in use.", "Another program has it open.", "Close that program and retry."),
            errno.EMFILE: ("Too many open files.", "The per-process file limit was reached.",
                           "Raise it with `ulimit -n 4096` and run the command again."),
        }
        if e in table:
            s, why, fix = table[e]
            return ActionableError(s + where, why=why, fix=fix, details=detail)
        return ActionableError(f"{exc.strerror or 'File system error'}{where}.", details=detail)
    if isinstance(exc, MemoryError):
        return ActionableError("Ran out of memory.",
                               why="The selection is larger than available RAM.",
                               fix="Scan fewer source folders at a time.", details=detail)
    return ActionableError(
        "Unexpected internal error.",
        why=f"{exc.__class__.__name__}: {exc}",
        fix="Run again with -vv and attach the log file path printed at the end when reporting the issue: "
            "https://github.com/dustalexw/datacurator/issues",
        details=detail)


def os_error_is_disk_full(exc: OSError) -> bool:
    """True for ENOSPC and the Windows ERROR_DISK_FULL (112) / ERROR_HANDLE_DISK_FULL (39)."""
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) in (112, 39)


__all__ = ["ActionableError", "ExitCode", "explain_exception", "os_error_is_disk_full"]
