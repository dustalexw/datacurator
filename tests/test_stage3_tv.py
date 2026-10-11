"""Stage 3b: complete Series–Season workflows, movies and providers."""
import os

from conftest import profile, rel
from fixtures import make_file


def plan(engine, lib, dest=None, top=None, **tv):
    t = {"root": str(dest) if dest else ""}
    t.update(tv)
    p = profile([lib], tv=t, **(top or {}))
    engine.scan(p)
    store = engine.plan(p)
    return p, store, {os.path.relpath(i.source, lib): i for i in store.items()}


def d(items, root):
    return {k: rel(v.dest, root) for k, v in items.items()}


def test_patterns_specials_multi_episode(engine, lib, tmp_path):
    out = tmp_path / "TV"
    for n in ["Show.S01E02.Pilot.Part.2.720p.mkv", "Show 1x03 Third.mkv", "Show.S01E04E05.1080p.mkv",
              "Show - S01E06-E08 - Finale.mkv", "Show.S00E01.Christmas.Special.mkv"]:
        make_file(str(lib / n), n.encode())
    _, _, items = plan(engine, lib, out)
    assert d(items, out) == {
        "Show.S01E02.Pilot.Part.2.720p.mkv": "Show/Season 01/Show - S01E02 - Pilot Part 2.mkv",
        "Show 1x03 Third.mkv": "Show/Season 01/Show - S01E03 - Third.mkv",
        "Show.S01E04E05.1080p.mkv": "Show/Season 01/Show - S01E04-E05.mkv",
        "Show - S01E06-E08 - Finale.mkv": "Show/Season 01/Show - S01E06-E08 - Finale.mkv",
        "Show.S00E01.Christmas.Special.mkv": "Show/Season 00/Show - S00E01 - Christmas Special.mkv",
    }
    assert "multi-episode" in items["Show - S01E06-E08 - Finale.mkv"].reason
    assert "Season 00" in items["Show.S00E01.Christmas.Special.mkv"].reason


def test_daily_show_by_date(engine, lib, tmp_path):
    make_file(str(lib / "Late.Night.2026-10-10.Guest.Name.720p.HDTV.mkv"))
    _, _, items = plan(engine, lib, tmp_path / "TV")
    assert d(items, tmp_path / "TV") == {
        "Late.Night.2026-10-10.Guest.Name.720p.HDTV.mkv": "Late Night/Season 2026/Late Night - 2026-10-10 - Guest Name.mkv"}


def test_absolute_numbering_requires_explicit_convention(engine, lib, tmp_path):
    make_file(str(lib / "[Grp] Long Anime - 1023 [1080p].mkv"))
    _, _, items = plan(engine, lib, tmp_path / "TV")
    it = next(iter(items.values()))
    assert it.action == "review" and "numbering convention" in it.reason
    _, _, items = plan(engine, lib, tmp_path / "TV", numbering="absolute")
    assert list(d(items, tmp_path / "TV").values()) == ["Long Anime/Long Anime - 1023.mkv"]


def test_subtitles_audio_and_metadata_follow_with_suffixes(engine, lib, tmp_path):
    base = "The.Show.S02E05.Episode.Name.1080p.WEB-DL"
    for ext in (".mkv", ".en.srt", ".en.forced.srt", ".pt-BR.sdh.srt", ".nfo", "-thumb.jpg", ".de.mka"):
        make_file(str(lib / f"dl/{base}{ext}"), ext.encode())
    out = tmp_path / "TV"
    _, _, items = plan(engine, lib, out)
    stem = "The Show/Season 02/The Show - S02E05 - Episode Name"
    assert d(items, out) == {f"dl/{base}{e}": f"{stem}{e}" for e in
                             (".mkv", ".en.srt", ".en.forced.srt", ".pt-BR.sdh.srt", ".nfo", "-thumb.jpg", ".de.mka")}
    assert all(i.role == "companion" for k, i in items.items() if not k.endswith(".mkv"))


def test_subs_folder_and_episode_code_matching(engine, lib, tmp_path):
    make_file(str(lib / "Movie.Night.S01E01.mkv"))
    make_file(str(lib / "Subs/English.srt"), b"en")
    make_file(str(lib / "Other.S01E01.en.srt"), b"x")  # different stem, same episode code
    out = tmp_path / "TV"
    _, _, items = plan(engine, lib, out)
    dd = d(items, out)
    assert dd["Subs/English.srt"] == "Movie Night/Season 01/Movie Night - S01E01.English.srt"
    assert dd["Other.S01E01.en.srt"] == "Movie Night/Season 01/Movie Night - S01E01.en.srt"


def test_series_from_folders_and_spelling_unified(engine, lib, tmp_path):
    make_file(str(lib / "Doctor Who (2005)/Season 2/S02E05.mkv"))
    make_file(str(lib / "dl/the expanse s01e01.mkv"))
    make_file(str(lib / "dl/The.Expanse.S01E02.mkv"))
    make_file(str(lib / "dl/The Expanse - S01E03.mkv"))
    out = tmp_path / "TV"
    _, _, items = plan(engine, lib, out, preset="series_year/season")
    dd = d(items, out)
    assert dd["Doctor Who (2005)/Season 2/S02E05.mkv"] == "Doctor Who (2005)/Season 02/Doctor Who - S02E05.mkv"
    assert {os.path.dirname(v) for k, v in dd.items() if "xpanse" in k} == {"The Expanse/Season 01"}


def test_ambiguous_series_year_goes_to_review(engine, lib, tmp_path):
    make_file(str(lib / "Doctor.Who.1963.S01E01.mkv"))
    make_file(str(lib / "Doctor.Who.2005.S01E01.mkv"))
    make_file(str(lib / "Doctor.Who.S01E02.mkv"))
    _, _, items = plan(engine, lib, tmp_path / "TV", preset="series_year/season")
    it = items["Doctor.Who.S01E02.mkv"]
    assert it.action == "review" and "1963" in it.reason and "2005" in it.reason


def test_episode_folders_rename_only_and_sort_only(engine, lib, tmp_path):
    make_file(str(lib / "in/Show.S01E01.Pilot.mkv"))
    make_file(str(lib / "in/Show.S01E01.Pilot.en.srt"))
    out = tmp_path / "TV"
    _, _, items = plan(engine, lib, out, preset="series/season/episode")
    assert d(items, out)["in/Show.S01E01.Pilot.mkv"] == "Show/Season 01/Show - S01E01 - Pilot/Show - S01E01 - Pilot.mkv"
    _, _, items = plan(engine, lib, out, mode="rename")
    assert d(items, lib) == {"in/Show.S01E01.Pilot.mkv": "in/Show - S01E01 - Pilot.mkv",
                             "in/Show.S01E01.Pilot.en.srt": "in/Show - S01E01 - Pilot.en.srt"}
    _, _, items = plan(engine, lib, out, mode="move")
    assert d(items, out) == {"in/Show.S01E01.Pilot.mkv": "Show/Season 01/Show.S01E01.Pilot.mkv",
                             "in/Show.S01E01.Pilot.en.srt": "Show/Season 01/Show.S01E01.Pilot.en.srt"}


def test_companion_conflict_holds_video_back(engine, lib, tmp_path):
    out = tmp_path / "TV"
    make_file(str(out / "Show/Season 01/Show - S01E01.en.srt"), b"existing different subtitle")
    make_file(str(lib / "Show.S01E01.mkv"), b"v")
    make_file(str(lib / "Show.S01E01.en.srt"), b"new subtitle")
    _, _, items = plan(engine, lib, out)
    assert items["Show.S01E01.en.srt"].action == "review"
    assert items["Show.S01E01.mkv"].action == "review" and "companion" in items["Show.S01E01.mkv"].reason


def test_nfo_metadata_used_offline(engine, lib, tmp_path):
    make_file(str(lib / "Show.S01E01.mkv"))
    make_file(str(lib / "Show.S01E01.nfo"),
              "<episodedetails><title>The Real Title</title><season>1</season><episode>1</episode></episodedetails>")
    _, _, items = plan(engine, lib, tmp_path / "TV")
    it = items["Show.S01E01.mkv"]
    assert it.dest.endswith("Show - S01E01 - The Real Title.mkv") and it.provenance["episode_title"] == "local .nfo"


def test_tv_apply_and_rerun_is_stable(engine, lib, tmp_path):
    for n in ["A.Show.S01E01.mkv", "A.Show.S01E01.en.srt", "A.Show.S01E02.mkv"]:
        make_file(str(lib / n), n.encode())
    p, store, items = plan(engine, lib)  # organize in place
    res = engine.apply(store)
    assert res.done == 3
    engine.scan(p)
    again = engine.plan(p)
    assert again.count("changes") == 0 and again.count("unchanged") == 3


def test_movies_with_companions_and_review(engine, lib, tmp_path):
    out = tmp_path / "Movies"
    make_file(str(lib / "Arrival.2016.2160p.UHD.BluRay.x265.mkv"))
    make_file(str(lib / "Arrival.2016.2160p.UHD.BluRay.x265.en.srt"))
    make_file(str(lib / "home video.mp4"))
    p = profile([lib], movies={"root": str(out)}, tv={"enabled": False})
    engine.scan(p)
    items = {os.path.basename(i.source): i for i in engine.plan(p).items()}
    assert rel(items["Arrival.2016.2160p.UHD.BluRay.x265.mkv"].dest, out) == "Arrival (2016)/Arrival (2016).mkv"
    assert rel(items["Arrival.2016.2160p.UHD.BluRay.x265.en.srt"].dest, out) == "Arrival (2016)/Arrival (2016).en.srt"
    assert items["home video.mp4"].action == "review"


# ------------------------------------------------------------------ provider (recorded responses)

class Recorded:
    """Simulated provider transport replaying recorded TVmaze-shaped responses."""

    def __init__(self):
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        if "/search/shows" in url and "Doctor" in url:
            return [{"score": 0.9, "show": {"id": 210, "name": "Doctor Who", "premiered": "2005-03-26",
                                            "network": {"name": "BBC One"}}},
                    {"score": 0.8, "show": {"id": 766, "name": "Doctor Who", "premiered": "1963-11-23",
                                            "network": {"name": "BBC One"}}}]
        if "/search/shows" in url:
            return [{"score": 0.9, "show": {"id": 1, "name": "The Expanse", "premiered": "2015-12-14",
                                            "network": {"name": "Syfy"}}}]
        if "/shows/1/episodes" in url:
            return [{"season": 1, "number": 2, "name": "The Big Empty", "airdate": "2015-12-15"}]
        if "/shows/210/episodes" in url:
            return [{"season": 1, "number": 1, "name": "Rose", "airdate": "2005-03-26"}]
        raise AssertionError(url)


def test_provider_fills_titles_with_provenance_and_caches(engine, lib, tmp_path):
    from datacurator.metadata.providers import CatalogCache, TVMazeProvider
    make_file(str(lib / "The.Expanse.S01E02.mkv"))
    rec = Recorded()
    prov = TVMazeProvider(transport=rec, cache=CatalogCache(engine.catalog), sleep=lambda s: None)
    p = profile([lib], tv={"root": str(tmp_path / "TV")})
    engine.scan(p)
    it = next(engine.plan(p, tv_provider=prov).items())
    assert it.dest.endswith("The Expanse - S01E02 - The Big Empty.mkv")
    assert it.provenance["episode_title"].startswith("tvmaze")
    n = len(rec.calls)
    engine.plan(p, tv_provider=TVMazeProvider(transport=rec, cache=CatalogCache(engine.catalog), sleep=lambda s: None))
    assert len(rec.calls) == n  # cached


def test_provider_ambiguity_requires_user_choice(engine, lib, tmp_path):
    from datacurator.metadata.providers import TVMazeProvider
    f = make_file(str(lib / "Doctor.Who.S01E01.mkv"))
    p = profile([lib], tv={"root": str(tmp_path / "TV")})
    engine.scan(p)
    it = next(engine.plan(p, tv_provider=TVMazeProvider(transport=Recorded(), sleep=lambda s: None)).items())
    assert it.action == "review" and "tvmaze:210" in it.suggestions["series_id"]
    engine.catalog.set_override(f, "series_id", "tvmaze:210")
    it = next(engine.plan(p, tv_provider=TVMazeProvider(transport=Recorded(), sleep=lambda s: None)).items())
    assert it.dest.endswith("Doctor Who - S01E01 - Rose.mkv")


def test_provider_failure_keeps_working_offline(engine, lib, tmp_path):
    from datacurator.metadata.providers import TVMazeProvider

    def down(url):
        raise OSError("network unreachable")
    make_file(str(lib / "Show.S01E01.Pilot.mkv"))
    p = profile([lib], tv={"root": str(tmp_path / "TV")})
    engine.scan(p)
    it = next(engine.plan(p, tv_provider=TVMazeProvider(transport=down, sleep=lambda s: None)).items())
    assert it.action == "move" and it.dest.endswith("Show - S01E01 - Pilot.mkv")
    assert "unavailable" in it.provenance["episode_title"]
