"""Core data structures shared by the scanner, planner, executor and UIs.

Nothing here touches the filesystem.
"""
from __future__ import annotations

import enum
from dataclasses import asdict, dataclass, field
from typing import Any


class Kind(str, enum.Enum):
    """Coarse file kind from extension only (no content inspection)."""
    AUDIO = "audio"
    VIDEO = "video"
    SUBTITLE = "subtitle"
    IMAGE = "image"
    DOCUMENT = "document"
    PLAYLIST = "playlist"
    CUE = "cue"
    TEXT = "text"
    NFO = "nfo"
    OTHER = "other"


class Category(str, enum.Enum):
    MUSIC = "music"
    TV = "tv"
    MOVIES = "movies"
    DOCUMENTS = "documents"
    PHOTOS = "photos"
    OTHER = "other"


class Action(str, enum.Enum):
    MOVE = "move"            # directory and/or name change
    NONE = "none"            # already satisfies the rules
    REVIEW = "review"        # uncertain: stays in place until the user decides
    SKIP = "skip"            # policy decided not to touch (conflict policy skip, duplicate, excluded)
    REWRITE = "rewrite"      # rewrite path references inside a companion (cue/m3u), explicit option only
    WRITE_TAGS = "write_tags"  # embedded tag change, explicit option only
    QUARANTINE = "quarantine"  # duplicate removal (explicitly approved dedupe plans only)


class Flag(str, enum.Enum):
    CONFLICT = "conflict"
    UNCERTAIN = "uncertain"
    DUPLICATE = "duplicate"
    ERROR = "error"
    SANITIZED = "sanitized"
    LIMITATION = "limitation"
    EXCLUDED = "excluded"


AUDIO_EXT = {".mp3", ".flac", ".m4a", ".m4b", ".aac", ".alac", ".ogg", ".oga", ".opus", ".wav",
             ".aif", ".aiff", ".aifc", ".wma", ".ape", ".wv", ".mpc", ".dsf", ".dff"}
VIDEO_EXT = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts",
             ".webm", ".flv", ".vob", ".divx", ".3gp"}
SUBTITLE_EXT = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup", ".smi", ".ttml"}
HEIF_EXT = {".heic", ".heif"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
             ".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".raf"} | HEIF_EXT
DOCUMENT_EXT = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".xls", ".xlsx", ".ods", ".csv",
                ".ppt", ".pptx", ".odp", ".md", ".pages", ".numbers", ".key", ".epub", ".tex"}
PLAYLIST_EXT = {".m3u", ".m3u8", ".pls"}
TEXT_EXT = {".txt", ".log", ".lrc", ".accurip", ".md5", ".sfv", ".ffp"}
# Alternate audio tracks that belong to a video when they share its stem
ALT_AUDIO_EXT = {".mka", ".ac3", ".dts", ".eac3", ".aac", ".flac", ".opus"}


def kind_for(ext: str) -> Kind:
    e = ext.lower()
    if e in AUDIO_EXT:
        return Kind.AUDIO
    if e in VIDEO_EXT:
        return Kind.VIDEO
    if e in SUBTITLE_EXT:
        return Kind.SUBTITLE
    if e in IMAGE_EXT:
        return Kind.IMAGE
    if e in PLAYLIST_EXT:
        return Kind.PLAYLIST
    if e == ".cue":
        return Kind.CUE
    if e == ".nfo":
        return Kind.NFO
    if e in TEXT_EXT:
        return Kind.TEXT
    if e in DOCUMENT_EXT:
        return Kind.DOCUMENT
    return Kind.OTHER


@dataclass(slots=True)
class FileRecord:
    """One scanned file. `meta` holds extracted metadata with provenance."""
    path: str                 # absolute path as found
    root: str                 # source root it was found under
    size: int
    mtime_ns: int
    ino: int
    dev: int
    nlink: int = 1
    kind: str = Kind.OTHER.value
    meta: dict[str, Any] = field(default_factory=dict)
    meta_error: str | None = None
    via_symlink: bool = False

    @property
    def stat_sig(self) -> dict[str, int]:
        return {"size": self.size, "mtime_ns": self.mtime_ns, "ino": self.ino, "dev": self.dev}


@dataclass(slots=True)
class SanitizeChange:
    component: str   # "folder" or "filename"
    before: str
    after: str
    reasons: list[str]


@dataclass(slots=True)
class PlanItem:
    id: int
    source: str
    dest: str | None
    category: str
    action: str
    reason: str
    role: str = "primary"          # primary | companion
    parent_id: int | None = None
    group: str = ""                # album / series / movie grouping key for batch overrides
    flags: list[str] = field(default_factory=list)
    conflict: str | None = None
    size: int = 0
    cross_device: bool = False
    limitations: list[str] = field(default_factory=list)
    sanitized: list[dict] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    suggestions: dict[str, Any] = field(default_factory=dict)
    src_sig: dict[str, int] = field(default_factory=dict)
    dest_key: str | None = None    # equivalence key on destination (case/normalization aware)
    dest_root: str | None = None
    content_hash: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)   # rewrite/tag payloads

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanItem":
        return cls(**d)

    @property
    def is_change(self) -> bool:
        return self.action in (Action.MOVE.value, Action.REWRITE.value, Action.WRITE_TAGS.value,
                               Action.QUARANTINE.value)


class CuratorError(Exception):
    """Base error with a plain-language message."""


class TemplateError(CuratorError):
    pass


class PreflightError(CuratorError):
    pass
