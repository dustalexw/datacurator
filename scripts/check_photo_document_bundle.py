"""Exercise HEIC/PDF preview, apply and undo using the packaged executable."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from PIL import Image
from pillow_heif import from_pillow


def check_bundle(cli):
    cli = str(Path(cli).resolve())
    with tempfile.TemporaryDirectory(prefix="dc-heic-pdf-") as temp:
        root = Path(temp)
        source = root / "source"
        source.mkdir()
        photo = source / "IMG_0412.HEIC"
        exif = Image.Exif()
        exif[306] = "2025:02:03 08:00:00"
        exif[0x8769] = {36867: "2021:07:04 10:00:00"}
        from_pillow(Image.new("RGB", (32, 32), (200, 100, 50))).save(photo, exif=exif.tobytes())
        pdf = source / "IMG_0412.PDF"
        pdf.write_bytes(b"%PDF-1.7\nreceipt")
        video = source / "IMG_0412.MOV"
        video.write_bytes(b"unclassified Live Photo video")
        originals = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (photo, pdf, video)}
        profile = root / "profile.json"
        profile.write_text(json.dumps({"sources": [str(source)], "categories": {
            "photos": {"enabled": True, "root": str(root / "Photos")},
            "documents": {"enabled": True, "root": str(root / "Documents")}}}))

        def run(*args):
            result = subprocess.run([cli, "--data-dir", str(root / "appdata"), "-q", *args],
                                    capture_output=True, text=True, timeout=60)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return result.stdout

        report = root / "plan.json"
        run("plan", "-p", str(profile), "--json", str(report))
        plan = json.loads(report.read_text())
        items = {Path(item["source"]): item for item in plan["items"]}
        targets = {photo: root / "Photos/2021/2021-07" / photo.name,
                   pdf: root / "Documents" / pdf.name}
        for path, category in ((photo, "photos"), (pdf, "documents")):
            item = items[path]
            assert item["category"] == category and item["action"] == "move", item
            assert Path(item["dest"]) == targets[path], item
        for path, (data, mtime) in originals.items():
            assert path.read_bytes() == data and path.stat().st_mtime_ns == mtime
        assert not (root / "Photos").exists() and not (root / "Documents").exists()
        output = run("apply", plan["header"]["plan_id"], "--yes")
        for path, target in targets.items():
            assert not path.exists() and target.read_bytes() == originals[path][0]
        assert video.read_bytes() == originals[video][0]
        batch = re.search(r"Undo with: curator undo (\S+)", output)
        assert batch, output
        run("undo", batch[1])
        for path, (data, _) in originals.items():
            assert path.read_bytes() == data
        assert all(not target.exists() for target in targets.values())
    print("Packaged HEIC/PDF: capture date, categories, read-only preview, exact-byte moves and undo passed")


if __name__ == "__main__":
    check_bundle(sys.argv[1])
