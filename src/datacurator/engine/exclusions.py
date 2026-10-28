"""Folders that must not be reorganized, shared by scanning, planning and duplicate finding.

Two kinds of folders are left alone:

* folders you exclude explicitly, and
* **dependent folders**: folders whose files only work while they stay where they are.
  Software projects, app and library bundles (Photos, Music, Final Cut, Logic …),
  creative projects (Ableton, Premiere, Pro Tools, InDesign …), installed games,
  virtual machines, disc structures (VIDEO_TS, BDMV), device backups, saved web
  pages and hidden settings folders. Moving one file out of these breaks the rest.

Detection is deliberately conservative near the top of your sources: a *source
folder itself* (and anything above it) is only protected by unambiguous signals
such as a Git repository, a library marker or a bundle suffix. Weaker signals —
a lone ``Makefile``, ``package.json``, ``index.html`` or project file — only
protect folders *inside* a source, so a stray download never freezes a whole
Downloads folder.

Use ``allow_dirs`` ("Organize anyway") to lift the automatic protection for a
specific folder; folders nested inside it that are themselves dependent (for
example its ``.git`` folder or an app bundle) stay protected.
"""
from __future__ import annotations

import os
from collections import OrderedDict

# ----------------------------------------------------------------- name based
GAME_LIBRARIES = {"steamapps", "epic games", "epicgames", "gog games", "xboxgames", "windowsapps",
                  "origin games", "ea games", "riot games", "ubisoft game launcher", "battle.net"}
GAME_MARKERS = {"steam_appid.txt", "steam_api.dll", "steam_api64.dll", "unityplayer.dll", "gameinfo.txt"}

# Folder suffixes for packages macOS shows as a single item, and other self-contained bundles.
BUNDLE_SUFFIXES = {
    ".app": "Application bundle", ".appex": "Application extension",
    ".photoslibrary": "Photos library", ".photolibrary": "iPhoto library", ".aplibrary": "Aperture library",
    ".musiclibrary": "Music library", ".tvlibrary": "TV library", ".imovielibrary": "iMovie library",
    ".theater": "iMovie theater", ".fcpbundle": "Final Cut Pro library", ".fcpxbundle": "Final Cut Pro library",
    ".logicx": "Logic Pro project", ".band": "GarageBand project", ".lrdata": "Lightroom catalog data",
    ".rtfd": "Rich-text document package", ".pages": "Pages document", ".numbers": "Numbers document",
    ".key": "Keynote document", ".scriv": "Scrivener project", ".dtbase2": "DEVONthink database",
    ".textbundle": "Text bundle", ".bundle": "Software bundle", ".framework": "Software framework",
    ".plugin": "Plug-in", ".kext": "System extension", ".prefpane": "Settings pane", ".qlgenerator": "Quick Look plug-in",
    ".saver": "Screen saver", ".mdimporter": "Spotlight plug-in", ".component": "Audio plug-in",
    ".vst": "Audio plug-in", ".vst3": "Audio plug-in", ".aaxplugin": "Audio plug-in", ".xpc": "Software component",
    ".xcodeproj": "Xcode project", ".xcworkspace": "Xcode workspace", ".playground": "Swift playground",
    ".xcassets": "Xcode asset catalog", ".dsym": "Debug symbols", ".pkg": "Installer package",
    ".mpkg": "Installer package", ".vmwarevm": "Virtual machine", ".pvm": "Virtual machine",
    ".utm": "Virtual machine", ".sparsebundle": "Disk image", ".mlpackage": "Machine-learning model",
    ".mlmodelc": "Machine-learning model", ".docarchive": "Documentation archive", ".abbu": "Contacts archive",
    ".ufo": "Font source", ".epub": "eBook package", ".lbaction": "LaunchBar action",
    ".alfredworkflow": "Alfred workflow", ".workflow": "Automator workflow", ".nib": "Interface file",
    ".storyboardc": "Interface file",
}

# Internal folders that are never organized (and are not worth descending into).
INTERNAL_NAMES = {
    ".git": "Git repository data", ".hg": "Mercurial repository data", ".svn": "Subversion data",
    ".bzr": "Bazaar repository data", "_darcs": "Darcs repository data",
    "node_modules": "Software dependencies", "bower_components": "Software dependencies",
    "__pycache__": "Python cache", ".venv": "Python environment", ".tox": "Python test environments",
    ".gradle": "Build cache", ".m2": "Build cache", "deriveddata": "Xcode build data",
    ".cache": "Application cache", ".npm": "Package cache", ".cargo": "Package cache", ".rustup": "Toolchain",
    "backups.backupdb": "Time Machine backup", ".trash": "Trash", ".trashes": "Trash",
    "video_ts": "DVD video structure", "audio_ts": "DVD audio structure", "bdmv": "Blu-ray structure",
    "avchd": "Camera video structure (AVCHD)",
}

# Operating-system and application-data folders (matter when a home or drive is a source).
SYSTEM_NAMES = {"appdata": "Windows app data", "application data": "Windows app data",
                "program files": "Installed programs", "program files (x86)": "Installed programs",
                "programdata": "Windows program data", "$windows.~bt": "Windows system files",
                "$windows.~ws": "Windows system files", "windows.old": "Windows system files",
                "system volume information": "Windows system files"}

# --------------------------------------------------------------- content based
# Strong signals: specific enough to protect even a source folder you chose.
STRONG_MARKERS = {
    ".git": "Software project (Git)", ".hg": "Software project (Mercurial)", ".svn": "Software project (Subversion)",
    ".bzr": "Software project (Bazaar)", "_darcs": "Software project (Darcs)",
    "pyvenv.cfg": "Python environment", "project.godot": "Godot project",
    "ableton project info": "Ableton Live project", ".obsidian": "Obsidian vault", "zotero.sqlite": "Zotero library",
    "itunes library.itl": "iTunes library", "itunes music library.xml": "iTunes library",
    "itunes library.xml": "iTunes library", "music library.musiclibrary": "Music library",
    "video_ts": "DVD video", "bdmv": "Blu-ray video", "avchd": "Camera video (AVCHD)",
    "backups.backupdb": "Time Machine backup", "plex media server": "Plex server data",
}
STRONG_EXTENSIONS = {".uproject": "Unreal project", ".vmx": "Virtual machine", ".vbox": "Virtual machine",
                     ".lrcat": "Lightroom catalog", ".cosessiondb": "Capture One session",
                     ".cocatalogdb": "Capture One catalog"}
# Weak signals: only protect folders inside a source, never the source itself.
WEAK_MARKERS = {
    "package.json": "Software project", "pyproject.toml": "Software project", "setup.py": "Software project",
    "requirements.txt": "Software project", "pipfile": "Software project", "cargo.toml": "Software project",
    "go.mod": "Software project", "pom.xml": "Software project", "build.gradle": "Software project",
    "build.gradle.kts": "Software project", "settings.gradle": "Software project",
    "cmakelists.txt": "Software project", "makefile": "Software project", "gnumakefile": "Software project",
    "configure.ac": "Software project", "meson.build": "Software project", "gemfile": "Software project",
    "composer.json": "Software project", "package.swift": "Software project", "podfile": "Software project",
    "pubspec.yaml": "Software project", "mix.exs": "Software project", "deno.json": "Software project",
    "tsconfig.json": "Software project", "dockerfile": "Software project", "docker-compose.yml": "Software project",
    "compose.yaml": "Software project", ".gitignore": "Software project", ".venv": "Software project",
    "node_modules": "Software project", ".vscode": "Software project", ".idea": "Software project", "manifest.json": "Browser extension or web app",
}
PROJECT_FILE_EXT = {
    ".als": "Ableton Live project", ".alp": "Ableton Live pack", ".rpp": "REAPER project", ".ptx": "Pro Tools session",
    ".ptf": "Pro Tools session", ".flp": "FL Studio project", ".cpr": "Cubase project", ".song": "Studio One song",
    ".sesx": "Audition session", ".prproj": "Premiere Pro project", ".aep": "After Effects project",
    ".aepx": "After Effects project", ".drp": "DaVinci Resolve project", ".kdenlive": "Kdenlive project",
    ".mlt": "Shotcut project", ".veg": "Vegas project", ".indd": "InDesign document", ".idml": "InDesign document",
    ".blend": "Blender project", ".c4d": "Cinema 4D project", ".ma": "Maya scene", ".mb": "Maya scene",
    ".max": "3ds Max scene", ".hip": "Houdini project", ".sln": "Visual Studio solution",
    ".csproj": "Visual Studio project", ".vcxproj": "Visual Studio project", ".tex": "LaTeX project",
    ".ipynb": "Notebook project", ".qgz": "QGIS project", ".qgs": "QGIS project", ".aup3": "Audacity project",
    ".aup": "Audacity project",
}
WEB_ENTRY = {"index.html", "index.htm", "index.php", "default.html", "default.htm"}
WEB_ASSET_DIRS = {"css", "js", "assets", "images", "img", "static", "scripts", "styles", "fonts", "media"}
WEB_ASSET_EXT = (".css", ".js")
SAVED_PAGE_SUFFIXES = ("_files", "-files", "-dateien", "_archivos", "_fichiers", "-bestanden")

# Reasons for folders that are never worth descending into, even when inventorying everything.
OPAQUE_REASONS = set(INTERNAL_NAMES.values()) | set(BUNDLE_SUFFIXES.values())


def _home() -> str:
    try:
        return os.path.realpath(os.path.expanduser("~"))
    except Exception:  # pragma: no cover
        return ""


class DirectoryExclusions:
    """Answers “should this folder be left alone, and why?” in plain language."""

    def __init__(self, settings=None, roots=None, managed=None):
        settings = settings or {}
        self.paths = [os.path.realpath(os.path.abspath(p)) for p in settings.get("exclude_dirs", [])]
        self.allow = {os.path.realpath(os.path.abspath(p)) for p in settings.get("allow_dirs", [])}
        # "exclude_games" is the setting's historical name; it now covers every dependent folder.
        self.games = settings.get("protect_dependents", settings.get("exclude_games", True))
        self.roots = [os.path.realpath(os.path.abspath(r)) for r in (roots or [])]
        # Data Curator's own destination folders: files it sorted there must not look like “a project”.
        self.managed = [os.path.realpath(os.path.abspath(r)) for r in (managed or [])]
        self.home = _home()
        self.cache: OrderedDict = OrderedDict()
        self.own_cache: OrderedDict = OrderedDict()

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _bounded(cache: OrderedDict, key, value):
        cache[key] = value
        if len(cache) > 8192:
            cache.popitem(last=False)
        return value

    def _inside_source(self, directory: str) -> bool:
        """True when `directory` is strictly inside one of the sources (not a source itself)."""
        if any(directory == m or directory.startswith(m.rstrip(os.sep) + os.sep) for m in self.managed):
            return False
        if not self.roots:
            return True  # callers without sources (legacy) get full detection
        return any(directory.startswith(r.rstrip(os.sep) + os.sep) for r in self.roots)

    def _listing(self, directory: str) -> dict[str, bool]:
        try:
            with os.scandir(directory) as it:
                out = {}
                for e in it:
                    try:
                        out[e.name.casefold()] = e.is_dir(follow_symlinks=False)
                    except OSError:
                        out[e.name.casefold()] = False
                return out
        except OSError:
            return {}

    def _game_reason(self, directory):
        """Kept for compatibility: the reason for this folder alone (no ancestors)."""
        return self._own(directory)

    def _own(self, directory: str) -> str | None:
        """Why this folder by itself (ignoring its parents) must stay intact, or None."""
        weak = self._inside_source(directory)
        key = (directory, weak)
        if key in self.own_cache:
            self.own_cache.move_to_end(key)
            return self.own_cache[key]
        return self._bounded(self.own_cache, key, self._detect(directory, weak))

    def _detect(self, directory: str, weak: bool) -> str | None:
        name = os.path.basename(directory)
        low = name.casefold()
        if not name:
            return None
        # names
        if low in GAME_LIBRARIES:
            return "Game library"
        suffix = os.path.splitext(low)[1]
        if suffix in BUNDLE_SUFFIXES and low != suffix:
            return BUNDLE_SUFFIXES[suffix]
        if low in INTERNAL_NAMES:
            return INTERNAL_NAMES[low]
        if low in SYSTEM_NAMES:
            return SYSTEM_NAMES[low]
        if low.startswith(".") and weak:
            return "Hidden app or settings folder"
        parent = os.path.dirname(directory)
        if low.endswith(SAVED_PAGE_SUFFIXES):
            stem = name.rsplit("_", 1)[0] if "_" in name else name.rsplit("-", 1)[0]
            for ext in (".html", ".htm", ".xhtml", ".shtml"):
                if os.path.isfile(os.path.join(parent, stem + ext)):
                    return "Saved web page files"
        # contents (a home folder or a drive is never “a project”)
        if directory in (self.home, os.path.realpath(os.sep)) or os.path.ismount(directory):
            return None
        names = self._listing(directory)
        if not names:
            return None
        if low == "library" and {"application support", "preferences"} <= set(names):
            return "macOS Library folder"
        if low == "windows" and "system32" in names:
            return "Windows system files"
        for marker, why in STRONG_MARKERS.items():
            if marker in names:
                return why
        if names.keys() & GAME_MARKERS or any(n.startswith("goggame-") and n.endswith(".info") for n in names):
            return "Installed game"
        if {"engine", "content", "binaries"} <= names.keys():
            return "Unreal Engine game"
        if any(n.endswith(".exe") for n in names) and any(n.endswith("_data") and d for n, d in names.items()):
            return "Unity game"
        if {"assets", "projectsettings"} <= names.keys():
            return "Unity project"
        if {"manifest.db", "info.plist", "status.plist"} <= names.keys():
            return "iPhone or iPad backup"
        if "metadata.db" in names and any(n.startswith("metadata_db_prefs") for n in names):
            return "Calibre library"
        for n in names:
            ext = os.path.splitext(n)[1]
            if ext in STRONG_EXTENSIONS:
                return STRONG_EXTENSIONS[ext]
        if not weak:
            return None
        for marker, why in WEAK_MARKERS.items():
            if marker in names:
                return why
        for n, is_dir in names.items():
            ext = os.path.splitext(n)[1]
            if ext in PROJECT_FILE_EXT and not n.startswith("."):
                return PROJECT_FILE_EXT[ext]
        if names.keys() & WEB_ENTRY and (
                any(d and n in WEB_ASSET_DIRS for n, d in names.items())
                or any(n.endswith(WEB_ASSET_EXT) for n in names)):
            return "Website"
        return None

    # ------------------------------------------------------------ public
    def explicit(self, directory: str) -> str | None:
        directory = os.path.realpath(os.path.abspath(directory))
        for path in self.paths:
            if directory == path or directory.startswith(path.rstrip(os.sep) + os.sep):
                return "Excluded folder"
        return None

    def reason(self, directory: str, opaque_only: bool = False) -> str | None:
        """Why `directory` (or a folder containing it) must be left alone, or None.

        With ``opaque_only`` only folders that are never worth looking inside (bundles,
        repository data, dependency caches) count; used when inventorying everything.
        """
        directory = os.path.realpath(os.path.abspath(directory))
        if why := self.explicit(directory):
            return why
        if not self.games:
            return None
        key = (directory, opaque_only)
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        reason = None
        current = directory
        while True:
            if current in self.allow:
                break  # "organize anyway": this folder and its parents are not judged
            own = self._own(current)
            if own and (not opaque_only or own in OPAQUE_REASONS):
                reason = own
                # report the outermost dependent folder: keep looking upwards
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return self._bounded(self.cache, key, reason)

    def protected_root(self, directory: str) -> tuple[str, str] | None:
        """The outermost dependent folder containing `directory`, with its reason."""
        directory = os.path.realpath(os.path.abspath(directory))
        if why := self.explicit(directory):
            return directory, why
        if not self.games:
            return None
        found = None
        current = directory
        while True:
            if current in self.allow:
                break
            if own := self._own(current):
                found = (current, own)
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return found
