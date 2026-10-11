"""Extension routing, protected folders, duplicate safety, and local/optional video metadata."""
import os
import threading

import pytest

from conftest import profile
from fixtures import make_audio, make_file
from datacurator import profiles as P
from datacurator.models import CuratorError
from datacurator.metadata.providers import TMDBMovieProvider, Candidate, choose, CatalogCache


def test_extension_rules_in_source_preview_apply_idempotence_undo(engine, lib):
    a = make_file(str(lib / "mixed/report.PDF"), b"%PDF-paper")
    b = make_file(str(lib / "mixed/archive.zip"), b"compressed")
    p = profile([lib], extension_folders={"enabled": True, "folders": {"PDF": "Paper/PDF", ".zip": "Archives"}})
    engine.scan(p)
    store = engine.plan(p)
    assert store.by_source(a).dest == str(lib / "Paper/PDF/report.PDF")
    assert store.by_source(b).dest == str(lib / "Archives/archive.zip")
    assert not (lib / "Archives").exists()
    result = engine.apply(store)
    assert result.done == 2
    engine.scan(p)
    assert engine.plan(p).summary()["changes"] == 0
    engine.undo(result.batch_id)
    assert os.path.exists(a) and os.path.exists(b)


@pytest.mark.parametrize("folder", ["../out", "/out", "x/../out", "C:/out", "{name}", "x//y", ""])
def test_extension_folder_validation(folder):
    with pytest.raises(CuratorError):
        P.normalize({"extension_folders": {"folders": {".pdf": folder}}})


def test_empty_custom_mapping_and_unknown_extension():
    assert P.normalize({"extension_folders": {"folders": {}}})["extension_folders"]["folders"] == {}
    with pytest.raises(CuratorError):
        P.normalize({"extension_folders": {"folders": {"*.pdf": "PDF"}}})


def test_format_folders_keep_music_companions(engine, lib):
    track = make_audio(str(lib / "album/1.flac"), title="One", artist="A", album="B", albumartist="A")
    cover = make_file(str(lib / "album/cover.jpg"))
    p = profile([lib], extension_folders={"enabled": True, "folders": {".flac": "FLAC", ".jpg": "Pictures"}})
    engine.scan(p)
    store = engine.plan(p)
    assert store.by_source(track).dest == str(lib / "FLAC/A/B/One.flac")
    assert store.by_source(cover).dest == str(lib / "FLAC/A/B/cover.jpg")


def test_game_and_manual_exclusions_also_protect_stale_plans(engine, lib):
    protected = [make_file(str(lib / "steamapps/common/Game/manual.pdf"), b"%PDF-game"),
                 make_file(str(lib / "Unity/manual.pdf"), b"%PDF-game"),
                 make_file(str(lib / "Saves/manual.pdf"), b"%PDF-game")]
    make_file(str(lib / "Unity/Game.exe"))
    (lib / "Unity/Game_Data").mkdir()
    p = profile([lib], scan={"exclude_games": False}, extension_folders={"enabled": True})
    engine.scan(p)
    p["scan"] = {"exclude_games": True, "exclude_dirs": [str(lib / "Saves")]}
    # Catalog entries scanned under earlier settings must also be protected without a rescan.
    store = engine.plan(p)
    assert all(store.by_source(path).action == "skip" and "excluded" in store.by_source(path).flags for path in protected)
    stats = engine.scan(p)
    assert len(stats.excluded_folders) == 3
    assert all(engine.catalog.get(path) is None for path in protected)
    # Selecting a nested game folder as the source still honors game protection.
    stats = engine.scan(profile([lib / "steamapps/common/Game"]))
    assert stats.files == 0 and stats.excluded_folders


def test_manual_exclusion_uses_real_path(engine, lib):
    protected = lib / "Protected"
    make_file(str(protected / "report.pdf"))
    os.symlink(protected, lib / "Alias")
    p = profile([lib], scan={"follow_symlinks": True, "exclude_dirs": [str(protected)]})
    stats = engine.scan(p)
    assert stats.files == 0


def test_duplicate_filters_preferred_keeper_selected_apply_and_undo(engine, lib):
    paths = [make_file(str(lib / folder / "copy.pdf"), b"identical PDF bytes") for folder in ("a", "b", "keep")]
    make_file(str(lib / "different.pdf"), b"differing PDF bytes")
    make_file(str(lib / "x.bin"), b"same binary")
    make_file(str(lib / "y.bin"), b"same binary")
    p = profile([lib], dedupe={"prefer_root": str(lib / "keep"), "quarantine_dir": str(lib / "Quarantine")})
    engine.scan(p)
    store = engine.find_duplicates(p, kind="document")
    assert store.count() == 2
    assert all(it.payload["keeper"] == paths[2] for it in store.items())
    chosen = next(store.items())
    result = engine.apply(store, approve_deletes=True, item_ids={chosen.id})
    assert result.done == 1 and os.path.exists(paths[2])
    assert sum(os.path.exists(path) for path in paths) == 2
    assert engine.scan(p).files == 5  # quarantine is omitted
    engine.undo(result.batch_id)
    assert all(os.path.exists(path) for path in paths)


def test_duplicate_exclusions_changed_keeper_and_cancel(engine, lib):
    for folder in ("a", "keep", "steamapps", "Excluded"):
        make_file(str(lib / folder / "x.bin"), b"same content")
    p = profile([lib], scan={"exclude_games": False})
    engine.scan(p)
    p["scan"] = {"exclude_dirs": [str(lib / "Excluded")]}
    p["dedupe"] = {"prefer_root": str(lib / "keep")}
    store = engine.find_duplicates(p)
    assert store.count() == 1
    (lib / "keep/x.bin").write_bytes(b"new content!")
    result = engine.apply(store, approve_deletes=True)
    assert result.done == 0 and (lib / "a/x.bin").exists()
    cancel = threading.Event(); cancel.set()
    with pytest.raises(InterruptedError):
        engine.find_duplicates(p, cancel=cancel)


def test_duplicate_quarantine_cannot_contain_source(engine, lib, tmp_path):
    with pytest.raises(ValueError, match="cannot"):
        engine.find_duplicates(profile([lib], dedupe={"quarantine_dir": str(tmp_path)}))


def test_duplicate_hardlinks_are_left_alone(engine, lib):
    make_file(str(lib / "a.bin"), b"same")
    os.link(lib / "a.bin", lib / "b.bin")
    p = profile([lib]); engine.scan(p)
    assert engine.find_duplicates(p).count() == 0


def test_local_nfo_generic_movie_and_tv_names(engine, lib):
    movie = make_file(str(lib / "Film/video.mkv"))
    make_file(str(lib / "Film/MOVIE.NFO"), b'<movie><title>Arrival</title><year>2016</year><plot>Local plot</plot><runtime>116</runtime><genre>Drama</genre><uniqueid type="tmdb">329865</uniqueid></movie>')
    episode = make_file(str(lib / "Series/Season 01/video.mkv"))
    make_file(str(lib / "Series/tvshow.nfo"), b'<tvshow><title>The Show</title><year>2020</year></tvshow>')
    make_file(str(lib / "Series/Season 01/video.nfo"), b'<episodedetails><season>1</season><episode>2</episode><episode>3</episode><title>Local title</title><plot>Episode plot</plot></episodedetails>')
    p = profile([lib]);engine.scan(p)
    store = engine.plan(p)
    item = store.by_source(movie)
    assert item.dest == str(lib / "Arrival (2016)/Arrival (2016).mkv")
    assert item.payload["metadata"]["plot"] == "Local plot" and item.payload["metadata"]["runtime"] == 116
    item = store.by_source(episode)
    assert item.dest == str(lib / "The Show/Season 01/The Show - S01E02-E03 - Local title.mkv")
    assert item.provenance["episode"] == "local .nfo"
    assert item.payload["metadata"]["plot"] == "Episode plot"


def test_embedded_mp4_movie_and_tv_tags(engine, lib):
    from mutagen.mp4 import MP4
    movie = make_audio(str(lib / "generic.m4v"), ext=".m4a", title="Arrival", date="2016-01-01")
    episode = make_audio(str(lib / "episode.mp4"), ext=".m4a", title="Pilot")
    tags = MP4(episode);tags["tvsh"] = ["My Show"];tags["tvsn"] = [1];tags["tves"] = [2];tags.save()
    p = profile([lib]);engine.scan(p);store = engine.plan(p)
    assert store.by_source(movie).dest.endswith("Arrival (2016)/Arrival (2016).m4v")
    assert store.by_source(episode).dest.endswith("My Show/Season 01/My Show - S01E02 - Pilot.mp4")
    assert store.by_source(episode).provenance["series"] == "embedded MP4 tags"


def test_optional_movie_lookup_cached_local_values_win(engine, lib):
    movie = make_file(str(lib / "Arrival.1080p.mkv"))
    p = profile([lib]);engine.scan(p)
    calls = []
    def transport(url):
        calls.append(url)
        if "/search/movie" in url:
            return {"total_pages": 1, "results": [{"id": 329865, "title": "Arrival", "release_date": "2016-11-11"}]}
        return {"title": "Arrival", "release_date": "2016-11-11", "overview": "Online plot", "runtime": 116}
    provider = TMDBMovieProvider(transport, cache=CatalogCache(engine.catalog), min_interval=0)
    item = engine.plan(p, movie_provider=provider).by_source(movie)
    assert item.dest.endswith("Arrival (2016)/Arrival (2016).mkv")
    assert item.payload["metadata"]["runtime"] == 116
    engine.plan(p, movie_provider=provider)
    assert len(calls) == 2
    make_file(str(lib / "Arrival.1080p.nfo"), b'<movie><title>Arrival</title><year>2016</year><plot>Local plot</plot></movie>')
    engine.scan(p)
    item = engine.plan(p, movie_provider=provider).by_source(movie)
    assert item.payload["metadata"]["plot"] == "Local plot"
    assert item.provenance["plot"] == "local .nfo"


def test_ambiguous_movies_require_choice_and_lookup_failure_keeps_local(engine, lib):
    movie = make_file(str(lib / "Dune.mkv"));p = profile([lib]);engine.scan(p)
    class Provider:
        name = "tmdb"
        def search(self, name):
            return [Candidate("tmdb", "1", "Dune", 1984), Candidate("tmdb", "2", "Dune", 2021)]
        def details(self, ref):
            return {"title": "Dune", "year": 2021, "plot": "Desert"}
    item = engine.plan(p, movie_provider=Provider()).by_source(movie)
    assert item.action == "review" and len(item.suggestions["_candidates"]) == 2
    engine.catalog.set_override(movie, "movie_id", "tmdb:2")
    item = engine.plan(p, movie_provider=Provider()).by_source(movie)
    assert item.dest.endswith("Dune (2021)/Dune (2021).mkv")
    assert choose([Candidate("tmdb", "1", "Dune", 1984)], "Dune", 2021).chosen is None
    class Unavailable(Provider):
        def details(self, ref):
            raise OSError("offline")
    engine.catalog.set_override(movie, "year", "2021")
    item = engine.plan(p, movie_provider=Unavailable()).by_source(movie)
    assert item.action == "move" and "unavailable" in item.provenance["lookup"]


def test_duplicate_keeper_is_checked_again_after_preflight(engine, lib):
    a = make_file(str(lib / "a.bin"), b"identical")
    b = make_file(str(lib / "b.bin"), b"identical")
    p = profile([lib]);engine.scan(p);store = engine.find_duplicates(p)
    def changed(event):
        # Progress runs after preflight and before the per-item move.
        with open(a, "wb") as f:
            f.write(b"modified!")
    result = engine.apply(store, approve_deletes=True, progress=changed)
    assert result.done == 0 and result.failed == 1 and os.path.exists(b)
    assert "retained copy changed" in result.errors[0][1]


def test_tv_specials_keep_filename_number_and_local_plot(engine, lib):
    from mutagen.mp4 import MP4
    episode = make_audio(str(lib / "Show.S00E01.mp4"), ext=".m4a", title="Local")
    tags = MP4(episode);tags["tvsh"] = ["Show"];tags["tvsn"] = [1];tags["tves"] = [2];tags.save()
    make_file(str(lib / "Show.S00E01.nfo"), b"<episodedetails><plot>Local plot</plot><runtime>30</runtime></episodedetails>")
    class Provider:
        name = "tvmaze"
        def search(self, name):
            return [Candidate("tvmaze", "1", "Show")]
        def episodes(self, ref):
            return {"S0E1": {"title": "Online title", "plot": "Online plot", "runtime": 40}}
    p = profile([lib]);engine.scan(p)
    item = engine.plan(p, tv_provider=Provider()).by_source(episode)
    assert "/Season 00/Show - S00E01 - Local.mp4" in item.dest
    assert item.payload["metadata"]["plot"] == "Local plot"
    assert item.payload["metadata"]["runtime"] == 30


def test_tmdb_pagination_is_bounded_and_auth_uses_header(monkeypatch):
    calls = []
    provider = TMDBMovieProvider(lambda url: calls.append(url) or {"total_pages": 4, "results": []}, min_interval=0)
    with pytest.raises(ValueError, match="Too many"):
        provider.search("Ambiguous")
    assert len(calls) == 3
    monkeypatch.setenv("DATACURATOR_TMDB_TOKEN", "test-read-token")
    class Response:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b'{"title":"Title"}'
    def urlopen(request, timeout):
        assert request.get_header("Authorization") == "Bearer test-read-token"
        assert "test-read-token" not in request.full_url
        assert timeout == 20
        return Response()
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert TMDBMovieProvider(min_interval=0).details("1")["title"] == "Title"
