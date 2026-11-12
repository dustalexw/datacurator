"""Interface-agnostic runtime services: errors, shutdown, progress and logging.

Nothing in this package imports the engine, the GUI or any third-party library, so
every front end (CLI, GUI worker, scripts, a future API) can share it.
"""
from .errors import ActionableError, ExitCode, explain_exception
from .lifecycle import ShutdownCoordinator
from .progress import (JsonLinesProgress, NullProgress, ProgressEvent, ProgressSink, TerminalProgress,
                       adapt_legacy_callback, format_bytes, format_duration)

__all__ = [
    "ActionableError", "ExitCode", "explain_exception", "ShutdownCoordinator",
    "ProgressEvent", "ProgressSink", "TerminalProgress", "JsonLinesProgress", "NullProgress",
    "adapt_legacy_callback", "format_bytes", "format_duration",
]
