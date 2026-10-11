"""Directory exclusions shared by scanning, planning and duplicate finding."""
import os
from collections import OrderedDict

GAME_LIBRARIES = {"steamapps", "epic games", "epicgames", "gog games", "xboxgames", "windowsapps",
                  "origin games", "ea games", "riot games", "ubisoft game launcher", "battle.net"}
GAME_MARKERS = {"steam_appid.txt", "steam_api.dll", "steam_api64.dll", "unityplayer.dll", "gameinfo.txt"}


class DirectoryExclusions:
    def __init__(self, settings=None):
        settings = settings or {}
        self.paths = [os.path.realpath(os.path.abspath(p)) for p in settings.get("exclude_dirs", [])]
        self.games = settings.get("exclude_games", True)
        self.cache = OrderedDict()

    def _game_reason(self, directory):
        if directory in self.cache:
            self.cache.move_to_end(directory)
            return self.cache[directory]
        name = os.path.basename(directory).casefold()
        reason = "Game library" if name in GAME_LIBRARIES else "Application bundle" if name.endswith(".app") else None
        if reason is None:
            try:
                names = {n.casefold() for n in os.listdir(directory)}
            except OSError:
                names = set()
            if names & GAME_MARKERS or any(n.startswith("goggame-") and n.endswith(".info") for n in names):
                reason = "Installed game"
            elif {"engine", "content", "binaries"} <= names:
                reason = "Unreal Engine game"
            elif any(n.endswith(".exe") for n in names) and any(n.endswith("_data") for n in names):
                reason = "Unity game"
        self.cache[directory] = reason
        if len(self.cache) > 4096:
            self.cache.popitem(last=False)
        return reason

    def reason(self, directory):
        directory = os.path.realpath(os.path.abspath(directory))
        for path in self.paths:
            if directory == path or directory.startswith(path.rstrip(os.sep) + os.sep):
                return "Excluded folder"
        if self.games:
            current = directory
            while True:
                reason = self._game_reason(current)
                if reason:
                    return reason
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
        return None
