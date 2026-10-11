import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from datacurator.engine.api import Engine  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    e = Engine(str(tmp_path / "appdata"))
    yield e
    e.close()


@pytest.fixture
def lib(tmp_path):
    d = tmp_path / "library"
    d.mkdir()
    return d


def profile(sources, **cats):
    """Small helper: profile({"music": {...}}) with sources."""
    p = {"sources": [str(s) for s in sources], "categories": {}}
    for k, v in cats.items():
        if k in ("music", "tv", "movies", "documents", "photos", "other"):
            p["categories"][k] = v
        else:
            p[k] = v
    return p


def plan_map(store, by="name"):
    """source -> item for convenience."""
    return {it.source: it for it in store.items()}


def rel(path, root):
    return os.path.relpath(path, str(root)) if path else None
