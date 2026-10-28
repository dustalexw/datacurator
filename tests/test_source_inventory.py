"""All-file source inventory and source-relative category destinations."""
import errno
import os
from pathlib import Path

import pytest

from fixtures import make_audio, make_file, make_photo
from datacurator import profiles as P
from datacurator.models import CuratorError


def test_downloads_inventory_includes_hidden_unknown_existing_destinations_and_games(engine, tmp_path):
    source = tmp_path / 'Downloads'; source.mkdir()
    paths = [make_audio(str(source / 'track.flac'), title='Song', artist='Band', album='Record', albumartist='Band'),
             make_photo(str(source / 'photo.jpg')),
             *[make_file(str(source / name)) for name in ('report.pdf', 'Arrival.2016.mkv', 'Show.S01E01.mkv',
                 'archive.zip', 'extensionless', '.hidden', '.hidden-folder/notes.xyz', 'nested/document.docx',
                 'Documents/organized.pdf', 'Music/note.xyz', 'steamapps/common/Game/manual.pdf',
                 'App.app/Contents/data.bin')]]
    before = {path: Path(path).read_bytes() for path in paths}
    profile = P.opening_profile([str(source)])
    stats = engine.scan(profile)
    bundle = str(source / 'App.app/Contents/data.bin')
    # Bundles are reported as protected without listing their insides; everything else is inventoried.
    assert stats.files == len(paths) - 1 and not stats.errors
    assert stats.excluded_folders == [(str(source / 'App.app'), 'Application bundle')]
    store = engine.plan(profile)
    assert {item.source for item in store.items()} == set(paths) - {bundle}
    roots = dict(zip(paths[:5], ('Music', 'Photos', 'Documents', 'Movies', 'TV')))
    for path, folder in roots.items():
        assert store.by_source(path).dest.startswith(str(source / folder) + os.sep)
    assert store.by_source(str(source / 'archive.zip')).dest == str(source / 'Other files/Archives/archive.zip')
    assert store.by_source(str(source / 'Documents/organized.pdf')).action == 'none'
    for name in ('steamapps/common/Game/manual.pdf', '.hidden-folder/notes.xyz'):
        item = store.by_source(str(source / name))
        assert item.action == 'skip' and 'excluded' in item.flags
    assert all(Path(path).read_bytes() == content for path, content in before.items())
    assert not (source / 'Other files').exists()  # preview creates no category folders


def test_multiple_sources_roundtrip_apply_rescan_and_undo(engine, tmp_path):
    sources = [tmp_path / name for name in ('Downloads', 'Another source')]
    paths = [make_file(str(source / 'receipt.pdf'), b'PDF-' + str(i).encode()) for i, source in enumerate(sources)]
    profile = P.opening_profile([str(p) for p in sources])
    saved = tmp_path / 'categories.json'; P.save(profile, str(saved)); profile = P.load(str(saved))
    engine.scan(profile); store = engine.plan(profile)
    assert [store.by_source(path).dest for path in paths] == [str(source / 'Documents/receipt.pdf') for source in sources]
    result = engine.apply(store)
    assert result.done == 2 and result.failed == 0
    assert engine.scan(profile).files == 2
    assert all(item.action == 'none' for item in engine.plan(profile).items())
    undone = engine.undo(result.batch_id)
    assert undone.complete and undone.restored == 2 and all(os.path.exists(path) for path in paths)


def test_all_inventory_includes_explicit_destination_without_recursing_app_catalog(engine, lib):
    plain = make_file(str(lib / 'plain.pdf'))
    output = make_file(str(lib / 'Output/already.pdf'))
    p = P.normalize({'sources': [str(lib)], 'categories': {'documents': {'root': str(lib / 'Output')}},
                     'scan': {'inventory_all': True}})
    assert engine.scan(p).files == 2
    p['scan']['inventory_all'] = False
    assert engine.scan(p).files == 1
    assert engine.catalog.get(plain) and engine.catalog.get(output) is None


def test_partial_permission_error_is_reported_and_preserves_catalog(engine, lib, monkeypatch):
    readable = make_file(str(lib / 'ok.pdf'))
    blocked = make_file(str(lib / 'Blocked/later.pdf'))
    p = P.opening_profile([str(lib)])
    assert engine.scan(p).files == 2
    from datacurator.engine import scanner
    real = scanner.os.scandir
    def scandir(path):
        if str(path) == str(lib / 'Blocked'):
            raise PermissionError(errno.EACCES, 'Permission denied', path)
        return real(path)
    monkeypatch.setattr(scanner.os, 'scandir', scandir)
    stats = engine.scan(p)
    assert stats.files == 1 and len(stats.errors) == 1 and 'Blocked' in stats.errors[0]
    assert engine.catalog.get(blocked) is not None and engine.catalog.get(readable) is not None


@pytest.mark.parametrize('folder', ['../Documents', '/Documents', 'C:/Documents', 'Documents//PDF', '{name}', '.'])
def test_source_subfolders_cannot_escape_the_source(folder):
    with pytest.raises(CuratorError, match='source_subfolder'):
        P.normalize({'categories': {'documents': {'source_subfolder': folder}}})


def test_legacy_profiles_keep_explicit_destinations_and_disabled_categories():
    p = P.normalize({'categories': {'documents': {'root': '/tmp/Documents', 'enabled': False}}})
    assert not p['categories']['documents']['enabled']
    assert not any(cfg.get('source_subfolder') for cfg in p['categories'].values())
    assert not p['scan'].get('inventory_all')


def test_category_symlink_outside_source_stays_in_review_without_writing_outside(engine, lib, tmp_path):
    source = make_file(str(lib / 'a.pdf'))
    external = tmp_path / 'External'; external.mkdir()
    os.symlink(external, lib / 'Documents')
    p = P.opening_profile([str(lib)])
    engine.scan(p); store = engine.plan(p)
    item = store.by_source(source)
    assert item.action == 'review' and 'links outside' in item.reason
    assert list(external.iterdir()) == []
