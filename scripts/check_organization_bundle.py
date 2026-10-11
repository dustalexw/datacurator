"""Exercise new folder/game/video/duplicate behavior through the packaged CLI."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def check_bundle(cli):
    cli = str(Path(cli).resolve())
    with tempfile.TemporaryDirectory(prefix="dc-organizer-") as temp:
        root = Path(temp)
        source = root / "source"; source.mkdir()
        def put(path, data):
            path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(data)
            return path
        pdf = put(source / "mix/a.pdf", b"%PDF-test")
        archive = put(source / "mix/a.zip", b"archive")
        video = put(source / "movie/video.mkv", b"video")
        put(source / "movie/movie.nfo", b"<movie><title>Arrival</title><year>2016</year><plot>Local plot</plot></movie>")
        protected = put(source / "steamapps/common/Game/manual.pdf", b"%PDF-game")
        profile = root / "profile.json"
        profile.write_text(json.dumps({"sources":[str(source)], "extension_folders":
            {"enabled":True, "folders":{".pdf":"PDF", ".zip":"Archives"}}}))
        def run(*args):
            result = subprocess.run([cli, "--data-dir", str(root / "appdata"), "-q", *args],
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout
        report = root / "plan.json"
        run("plan", "-p", str(profile), "--json", str(report))
        plan = json.loads(report.read_text());items = {Path(i["source"]): i for i in plan["items"]}
        assert protected not in items
        assert Path(items[pdf]["dest"]) == source / "PDF/a.pdf"
        assert Path(items[archive]["dest"]) == source / "Archives/a.zip"
        assert Path(items[video]["dest"]) == source / "Arrival (2016)/Arrival (2016).mkv"
        assert items[video]["payload"]["metadata"]["plot"] == "Local plot"
        assert not (source / "PDF").exists()
        output = run("apply", plan["header"]["plan_id"], "--yes")
        batch = re.search(r"Undo with: curator undo (\S+)", output);assert batch, output
        assert protected.read_bytes() == b"%PDF-game"
        run("undo", batch[1]);assert pdf.exists() and archive.exists() and video.exists()
        duplicate = put(source / "other/a.pdf", pdf.read_bytes())
        quarantine = root / "quarantine"
        output = run("dedupe", "-p", str(profile), "--quarantine", str(quarantine))
        plan_id = re.search(r"Apply with: curator apply (\S+)", output);assert plan_id, output
        output = run("apply", plan_id[1], "--approve-deletes", "--yes")
        batch = re.search(r"Undo with: curator undo (\S+)", output);assert batch, output
        assert sum(p.exists() for p in (pdf, duplicate)) == 1
        run("undo", batch[1]);assert pdf.exists() and duplicate.exists()
    print("Packaged extension routing, game exclusion, local NFO parsing, duplicate quarantine and undo passed")


if __name__ == "__main__":
    check_bundle(sys.argv[1])
