"""Real HEIF metadata, photo/document routing, safe moves and upgrade rescans."""
import os

import pytest

from conftest import profile
from fixtures import make_audio, make_file, make_heif, snapshot
from datacurator.storage.catalog import META_VERSION


@pytest.mark.parametrize("extension", [".heic", ".HEIC", ".heif"])
def test_heif_preview_apply_undo_preserves_original(engine, lib, tmp_path, extension):
    source = lib / ("IMG_0412" + extension)
    make_heif(str(source))
    original = source.read_bytes()
    before = snapshot(str(lib))
    dest = tmp_path / "Photos"
    p = profile([lib], photos={"enabled": True, "root": str(dest)})
    engine.scan(p)
    record = engine.catalog.get(str(source))
    assert record.meta_error is None
    assert (record.meta["year"], record.meta["month"], record.meta["day"]) == (2021, 7, 4)
    assert record.meta["camera_make"] == "Apple"
    store = engine.plan(p)
    item = store.by_source(str(source))
    target = dest / "2021/2021-07" / source.name
    assert item.category == "photos" and item.action == "move"
    assert item.dest == str(target) and item.provenance["date"] == "EXIF capture date"
    assert snapshot(str(lib)) == before and not dest.exists()
    result = engine.apply(store)
    assert result.status == "completed" and result.done == 1
    assert target.read_bytes() == original and not source.exists()
    undo = engine.undo(result.batch_id)
    assert undo.restored == 1 and source.read_bytes() == original and not target.exists()


def test_upgrade_retries_cached_heic_failures(engine, lib, tmp_path):
    source = lib / "IMG_0412.HEIC"
    make_heif(str(source))
    p = profile([lib], photos={"enabled": True, "root": str(tmp_path / "Photos")})
    engine.scan(p)
    engine.catalog.db.execute(
        "UPDATE files SET meta='{}', meta_error='Could not read image metadata: UnidentifiedImageError', "
        "meta_version=? WHERE path=?", (META_VERSION - 1, str(source)))
    assert next(engine.plan(p).items()).action == "review"
    stats = engine.scan(p)
    assert stats.extracted == 1 and stats.reused == 0
    assert next(engine.plan(p).items()).action == "move"
    assert engine.scan(p).reused == 1


def test_undated_heic_waits_for_explicit_fallback(engine, lib, tmp_path):
    source = lib / "undated.heic"
    make_heif(str(source), date=None)
    os.utime(source, (1609459200, 1609459200))
    p = profile([lib], photos={"enabled": True, "root": str(tmp_path / "Photos")})
    engine.scan(p)
    item = next(engine.plan(p).items())
    assert item.action == "review" and "capture date" in item.reason
    p["categories"]["photos"]["date_fallback"] = "mtime"
    item = next(engine.plan(p).items())
    assert item.action == "move" and "file modified time" in item.provenance["date"]


def test_corrupt_heic_explains_metadata_failure(engine, lib, tmp_path):
    source = lib / "broken.heic"
    make_file(str(source), b"not a HEIC container")
    p = profile([lib], photos={"enabled": True, "root": str(tmp_path / "Photos")})
    engine.scan(p)
    item = next(engine.plan(p).items())
    assert item.action == "review" and "Could not read image metadata" in item.reason
    assert source.read_bytes() == b"not a HEIC container"


def test_mixed_folder_routes_heic_and_pdf_to_selected_categories(engine, lib, tmp_path):
    make_heif(str(lib / "IMG_0412.HEIC"))
    make_file(str(lib / "IMG_0412.MOV"), b"live photo video")
    make_file(str(lib / "IMG_0412.PDF"), b"%PDF-1.7\nreceipt")
    make_file(str(lib / "invoice.pdf"), b"%PDF-1.7\ninvoice")
    make_audio(str(lib / "track.flac"), title="One", artist="A", album="B", albumartist="A", tracknumber="1")
    p = profile([lib], photos={"enabled": True, "root": str(tmp_path / "Photos")},
                documents={"enabled": True, "root": str(tmp_path / "Documents")},
                music={"root": str(tmp_path / "Music")})
    before = snapshot(str(lib))
    engine.scan(p)
    store = engine.plan(p)
    assert snapshot(str(lib)) == before
    for name, category in (("IMG_0412.HEIC", "photos"), ("IMG_0412.PDF", "documents"),
                           ("invoice.pdf", "documents")):
        item = store.by_source(str(lib / name))
        assert item.category == category and item.role != "companion" and item.action == "move"
    originals = {name: (lib / name).read_bytes() for name in ("IMG_0412.HEIC", "IMG_0412.PDF", "invoice.pdf")}
    result = engine.apply(store)
    assert result.status == "completed"
    for name, data in originals.items():
        target = store.by_source(str(lib / name)).dest
        with open(target, "rb") as f:
            assert f.read() == data
    assert (lib / "IMG_0412.MOV").exists()  # unrelated, unclassified video stays for review
    assert engine.undo(result.batch_id).restored == result.done


def test_heic_disabled_category_stays_in_place(engine, lib, tmp_path):
    source = lib / "IMG_0412.HEIC"
    make_heif(str(source))
    p = profile([lib], photos={"enabled": False, "root": str(tmp_path / "Photos")})
    engine.scan(p)
    assert next(engine.plan(p).items()).action == "skip"
    assert source.exists()
