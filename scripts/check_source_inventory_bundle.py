"""Exercise source-relative category organization through the packaged CLI."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def check_bundle(cli):
    cli = str(Path(cli).resolve())
    with tempfile.TemporaryDirectory(prefix='dc-source-inventory-') as temp:
        root = Path(temp)
        sources = [root / 'Downloads', root / 'Second source']
        paths = []
        for i, source in enumerate(sources):
            for name in ('receipt.pdf', 'archive.zip', '.hidden', 'extensionless', 'nested/report.docx',
                         'Documents/organized.pdf', 'App.app/Contents/data.bin'):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f'{i}: {name}'.encode())
                paths.append(path)
        before = {path: path.read_bytes() for path in paths}
        profile = root / 'profile.json'
        profile.write_text(json.dumps({'sources': [str(s) for s in sources],
            'categories': {cat: {'enabled': True, 'source_subfolder': folder} for cat, folder in
                {'music': 'Music', 'tv': 'TV', 'movies': 'Movies', 'documents': 'Documents',
                 'photos': 'Photos', 'other': 'Other files'}.items()},
            'scan': {'inventory_all': True}}))
        def run(*args):
            result = subprocess.run([cli, '--data-dir', str(root / 'catalog'), '-q', *args],
                                    capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout
        report = root / 'plan.json'
        run('plan', '-p', str(profile), '--json', str(report))
        plan = json.loads(report.read_text())
        items = {Path(item['source']): item for item in plan['items']}
        assert set(items) == set(paths)
        for source in sources:
            assert Path(items[source / 'receipt.pdf']['dest']) == source / 'Documents/receipt.pdf'
            assert Path(items[source / 'archive.zip']['dest']) == source / 'Other files/archive.zip'
            assert items[source / 'Documents/organized.pdf']['action'] == 'none'
            assert items[source / 'App.app/Contents/data.bin']['action'] == 'skip'
        assert all(path.read_bytes() == contents for path, contents in before.items())
        assert all(not (source / 'Other files').exists() for source in sources)
        result = run('apply', plan['header']['plan_id'], '--yes')
        batch = re.search(r'Undo with: curator undo (\S+)', result)
        assert batch, result
        for source in sources:
            assert (source / 'Documents/receipt.pdf').exists()
            assert (source / 'Other files/.hidden').exists()
            assert (source / 'App.app/Contents/data.bin').exists()
        run('plan', '-p', str(profile), '--json', str(report))
        again = json.loads(report.read_text())
        assert all(item['action'] in ('none', 'skip') for item in again['items'])
        run('undo', batch[1])
        assert all(path.read_bytes() == contents for path, contents in before.items())
    print('Packaged all-file inventory, category destinations, two-source apply/rescan and undo passed')


if __name__ == '__main__':
    check_bundle(sys.argv[1])
