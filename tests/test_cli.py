"""The CLI drives the same engine; every documented command runs."""
import json
import os

import pytest

from fixtures import make_audio, make_file
from datacurator.cli import main


@pytest.fixture
def cli(tmp_path, monkeypatch):
    data = str(tmp_path / "app")
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    def run(*args):
        return main(["-q", "--data-dir", data, *map(str, args)])
    return run


def latest_plan(tmp_path):
    d = tmp_path / "app/plans"
    names = sorted((f for f in os.listdir(d) if f.endswith(".sqlite")), key=lambda f: os.stat(d / f).st_ctime_ns)
    return names[-1][len("plan-"):-len(".sqlite")]


def test_full_cli_workflow(cli, tmp_path, capsys):
    lib = tmp_path / "lib"
    make_audio(str(lib / "a/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    for i, a in enumerate("XYZ", 1):
        make_audio(str(lib / f"mix/{i}.flac"), title=f"S{i}", artist=a, album="Mix", tracknumber=str(i))
    make_file(str(lib / "Show.S01E01.mkv"))
    prof = tmp_path / "p.json"
    assert cli("init-profile", prof, "-s", lib, "--music-root", tmp_path / "M", "--tv-root", tmp_path / "T") == 0
    assert cli("plan", "-p", prof, "--show", "review", "--csv", tmp_path / "r.csv", "--json", tmp_path / "r.json") == 0
    out = capsys.readouterr().out
    assert "review: 3" in out and "Nothing has been changed" in out
    assert not (tmp_path / "M").exists()
    pid = latest_plan(tmp_path)
    assert cli("show", pid, "--filter", "review") == 0
    assert cli("explain", pid, "2") == 0
    review_ids = [it["id"] for it in json.load(open(tmp_path / "r.json"))["items"] if it["action"] == "review"]
    assert cli("accept", pid, *review_ids) == 0
    assert cli("plan", "-p", prof, "--no-scan") == 0
    assert "changes: 5" in capsys.readouterr().out
    assert cli("apply", latest_plan(tmp_path), "-y") == 0
    assert (tmp_path / "M/Various Artists/Mix/01 - S1.flac").exists()
    assert cli("history") == 0
    batch = capsys.readouterr().out.strip().splitlines()[-1].split()[0]
    assert cli("undo", batch, "--dry-run") == 0
    assert cli("undo", batch) == 0
    assert (lib / "mix/1.flac").exists()
    assert cli("recover") == 0


def test_cli_misc_commands(cli, tmp_path, capsys):
    lib = tmp_path / "lib"
    f = make_file(str(lib / "doc.pdf"), b"%PDF")
    make_file(str(lib / "dup1.bin"), b"same")
    make_file(str(lib / "dup2.bin"), b"same")
    prof = tmp_path / "p.json"
    cli("init-profile", prof, "-s", lib)
    assert cli("scan", "-p", prof) == 0
    assert cli("probe", tmp_path) == 0
    assert cli("presets") == 0
    assert cli("template", "tv", "--folder", "{series}/[Season {season:02}]", "--filename", "{series} - {episode_code}") == 0
    assert "The Expanse/Season 01/The Expanse - S01E02.mkv" in capsys.readouterr().out
    assert cli("override", f, "name=renamed") == 0
    assert cli("exclude", f) == 0
    assert cli("exclude", f, "--include") == 0
    assert cli("tag", "add", "taxes", f) == 0
    assert cli("collection", "add", "Paperwork", f) == 0
    assert cli("collection", "list") == 0
    assert cli("search", "--tag", "taxes") == 0
    assert f in capsys.readouterr().out
    assert cli("locations", "add", "lib", lib) == 0
    assert cli("locations") == 0
    assert "Reachable" in capsys.readouterr().out
    assert cli("dedupe", "-p", prof, "--quarantine", tmp_path / "Q", "--no-scan") == 0
    pid = latest_plan(tmp_path)
    assert cli("apply", pid, "-y") == 4  # needs --approve-deletes
    assert cli("apply", pid, "-y", "--approve-deletes") == 0
    assert cli("report", pid, "--csv", tmp_path / "d.csv") == 0


def test_cli_unattended_gate(cli, tmp_path, capsys):
    lib = tmp_path / "lib"
    make_audio(str(lib / "in/1.flac"), title="T", artist="A", album="B", albumartist="A", tracknumber="1")
    prof = tmp_path / "p.json"
    cli("init-profile", prof, "-s", lib)
    assert cli("run", "-p", prof) == 1
    assert "not enabled for unattended" in capsys.readouterr().err
    p = json.load(open(prof))
    p["unattended"] = {"enabled": True, "conflict_policy": "skip"}
    json.dump(p, open(prof, "w"))
    assert cli("run", "-p", prof) == 0
    assert (lib / "A/B/01 - T.flac").exists()
    assert cli("run", "-p", prof) == 0
    assert "Nothing to do" in capsys.readouterr().out
