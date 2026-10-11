"""Optional, replaceable online metadata providers.

Organization works fully offline; a provider only *adds* episode titles when the
user enables it. Every value it contributes is labelled with its provenance, and
ambiguous matches are never auto-picked: they go to the review queue with the
candidates for the user to choose (override ``series_id``).

Responses are cached in the catalog so repeated plans do not repeat requests,
and requests are rate-limited.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class Candidate:
    provider: str
    id: str
    name: str
    year: int | None = None
    detail: str = ""
    score: float = 0.0

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.id}"

    def label(self) -> str:
        return f"{self.name}" + (f" ({self.year})" if self.year else "") + (f" — {self.detail}" if self.detail else "") \
            + f" [{self.ref}]"


@dataclass
class Match:
    chosen: Candidate | None
    candidates: list[Candidate] = field(default_factory=list)
    reason: str = ""


class TVProvider(Protocol):
    name: str

    def search(self, series: str) -> list[Candidate]: ...

    def episodes(self, show_id: str) -> dict[str, dict[str, Any]]: ...


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", s.casefold()).split())


def choose(cands: list[Candidate], name: str, year: int | None) -> Match:
    """Pick only when unambiguous: one exact-name candidate (and matching year if known)."""
    exact = [c for c in cands if _norm(c.name) == _norm(name)]
    if year:
        exact_y = [c for c in exact if c.year == year]
        if len(exact_y) == 1:
            return Match(exact_y[0], cands, "exact name and year")
    if len(exact) == 1:
        return Match(exact[0], cands, "exact name")
    if not cands:
        return Match(None, [], "no match found")
    return Match(None, (exact or cands)[:6], f"{len(exact) or len(cands)} possible matches")


class TVMazeProvider:
    """TVmaze public API (no key). `transport(url) -> parsed JSON` is injectable for tests."""

    name = "tvmaze"
    base = "https://api.tvmaze.com"

    def __init__(self, transport: Callable[[str], Any] | None = None, cache=None,
                 min_interval: float = 0.5, sleep=time.sleep):
        self.transport = transport or self._http
        self.cache = cache  # object with get(key) / put(key, value)
        self.min_interval = min_interval
        self.sleep = sleep
        self._last = 0.0
        self._lock = threading.Lock()

    def _http(self, url: str) -> Any:  # pragma: no cover - network
        req = urllib.request.Request(url, headers={"User-Agent": "DataCurator/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def _get(self, path: str) -> Any:
        key = f"{self.name}:{path}"
        if self.cache is not None:
            hit = self.cache.get(key)
            if hit is not None:
                return hit
        with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            data = self.transport(self.base + path)
        if self.cache is not None:
            self.cache.put(key, data)
        return data

    def search(self, series: str) -> list[Candidate]:
        data = self._get("/search/shows?q=" + urllib.parse.quote(series)) or []
        out = []
        for row in data:
            s = row.get("show") or {}
            prem = s.get("premiered") or ""
            net = (s.get("network") or s.get("webChannel") or {}).get("name") or ""
            out.append(Candidate(self.name, str(s.get("id")), s.get("name") or "",
                                 int(prem[:4]) if prem[:4].isdigit() else None, net, float(row.get("score") or 0)))
        return out

    def episodes(self, show_id: str) -> dict[str, dict[str, Any]]:
        data = self._get(f"/shows/{show_id}/episodes?specials=1") or []
        out: dict[str, dict[str, Any]] = {}
        for ep in data:
            title = ep.get("name")
            s, n = ep.get("season"), ep.get("number")
            if ep.get("type") == "significant_special" or n is None:
                continue
            out[f"S{s}E{n}"] = {"title": title, "airdate": ep.get("airdate")}
            if ep.get("airdate"):
                out.setdefault(f"D{ep['airdate']}", {"title": title, "season": s, "episode": n})
        return out


class CatalogCache:
    """Provider response cache stored in the catalog database."""

    def __init__(self, catalog, max_age: float = 30 * 86400):
        self.db = catalog.db
        self.lock = catalog._lock
        self.max_age = max_age
        with self.lock:
            self.db.execute("CREATE TABLE IF NOT EXISTS provider_cache(key TEXT PRIMARY KEY, value TEXT, fetched REAL)")

    def get(self, key: str):
        with self.lock:
            r = self.db.execute("SELECT value, fetched FROM provider_cache WHERE key=?", (key,)).fetchone()
        if r and time.time() - r[1] < self.max_age:
            return json.loads(r[0])
        return None

    def put(self, key: str, value) -> None:
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO provider_cache VALUES(?,?,?)", (key, json.dumps(value), time.time()))


PROVIDERS: dict[str, type] = {"tvmaze": TVMazeProvider}


def make_tv_provider(name: str | None, catalog=None, transport=None):
    if not name or name == "none":
        return None
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f"Unknown TV metadata provider {name!r}")
    return cls(transport=transport, cache=CatalogCache(catalog) if catalog is not None else None)
