# Data Curator

A desktop application (and matching command-line tool) that organizes and renames music, TV, movies,
documents and photos into a **minimal, deterministic folder structure** — safely, with a preview,
a durable recovery journal and undo.

```
Choose sources → Choose rules → Scan → Review proposed changes → Apply → View results or undo
```

* **Nothing changes until you apply.** Scanning and preview only read files (verified by tests that
  snapshot every byte and timestamp before and after a preview).
* **Only the folders the rules need.** `Music/Album Artist/Album/01 - Title.flac`,
  `TV/Series/Season 01/Series - S01E01 - Title.mkv`. Disc, year, resolution or episode folders appear
  only when a chosen rule asks for them. Tags, collections, search and the review queue live in the
  catalog, never as extra folders.
* **Deterministic.** The same files and settings always produce the same plan (input is processed in
  sorted order; every tie has a stable rule). Re-running an applied plan proposes no changes.
* **Uncertain files stay put.** Missing album artist, conflicting tags, untagged multi-artist albums,
  bare episode numbers, videos that are neither clearly an episode nor a movie — all go to the
  review queue with a suggested fix. Metadata is never invented.
* **Never overwrites.** No-replace renames on every platform; cross-drive moves copy to a temporary
  name, verify a BLAKE2b hash, finalize atomically and only then remove the original.
* **Works offline.** Embedded tags, filenames and local `.nfo` files. Online enrichment (TVmaze) is
  optional, cached, and labelled with its provenance.

![Review screen](docs/screenshots/3-review.png)

## Install

Requires Python 3.11+.

```bash
python -m pip install "datacurator[gui] @ ."        # from this folder
# or for development:
python -m pip install -e ".[gui,dev]"
```

Then run the desktop app with `datacurator` or the CLI with `curator --help`.
Standalone builds (no Python needed) are described in [docs/PACKAGING.md](docs/PACKAGING.md).

## Desktop app

1. **Sources** — add folders (local, removable, mapped drives, UNC paths, mounted shares) or saved
   locations, and pick a destination per kind of file. Leave a destination empty to organize *inside*
   the source folder without a wrapper folder. **Check** measures a destination (case sensitivity,
   Unicode behaviour, Windows name rules, links, free space) with a hidden test file.
2. **Rules** — choose a layout preset per category or edit the templates; the example updates as you
   type, including what happens when values are missing. Collision handling and naming preset are
   visible; everything else is under **Advanced**.
3. **Review** — a virtualized before/after table with search and filters for the review queue,
   conflicts, uncertain matches, duplicates, errors, name adjustments and preservation notes; the
   proposed folder tree; per-file explanations in plain language, including metadata provenance and
   every sanitization. Batch actions: *Leave alone*, *Accept suggestion*, *Set value…* (per file or
   for the whole album/series), *Update preview*. Export the plan as CSV/JSON.
4. **Results** — progress with pause/cancel, the run history, undo with a dry-run summary, and
   recovery of interrupted runs (offered automatically on start-up).

## Command line

The CLI uses the same engine.

```bash
curator init-profile my.json -s ~/Downloads --music-root ~/Music --tv-root ~/Videos/TV
curator plan -p my.json --show review          # scan + preview; changes nothing
curator explain <plan-id> 12                   # why item #12 is proposed
curator accept <plan-id> 12 13 14              # turn suggestions into catalog overrides
curator override ~/Downloads/x.flac album_artist="Prince"
curator apply <plan-id>                        # asks for confirmation; Ctrl-C cancels safely
curator undo <batch-id> --dry-run
curator recover --resume                       # after a crash, power loss or disconnect
curator probe /Volumes/NAS/Music               # measure a destination
curator locations add nas /mnt/nas --protocol smb --server nas.local --share media --user me --password-prompt
curator dedupe -p my.json --quarantine ~/Quarantine   # separate, explicitly approved
curator run -p nightly.json                    # unattended; profile must enable it
```

Unattended runs (`curator run`) refuse profiles that do not set
`"unattended": {"enabled": true, "conflict_policy": "skip" | "alternate"}`. Schedule them with the
OS scheduler (cron, systemd timers, launchd, Task Scheduler).

Example profiles: [`profiles/`](profiles). Template reference: [docs/TEMPLATES.md](docs/TEMPLATES.md).

## Architecture

| Layer | Module | Notes |
|---|---|---|
| Interfaces | `gui/` (PySide6), `cli.py` | Thin; never touch files directly |
| Engine facade | `engine/api.py` | scan → plan → apply / undo / recover |
| Scanner | `engine/scanner.py` | Read-only, incremental (size+mtime+inode), bounded thread pool (fewer threads on network shares), symlink-cycle and mount-boundary aware |
| Metadata | `metadata/` | `mutagen` (MP3, FLAC, M4A/AAC/ALAC, Ogg Vorbis, Opus, WAV, AIFF, WMA, APE, WavPack, Musepack), Pillow EXIF, Kodi `.nfo`, filename parsers; replaceable online providers |
| Planner | `engine/planner.py` | Deterministic rules, album/series grouping, companions, collisions, sanitization, preservation notes |
| Plan store | `engine/planstore.py` | Plans are SQLite files: paged UI, streamed execution, CSV/JSON export |
| Executor | `engine/executor.py` | Preflight, staging for rename cycles and case-only renames, no-replace renames, verified copies, retries with bounded backoff, pause/cancel |
| Journal / undo | `engine/journal.py`, `engine/undo.py` | fsync'd SQLite journal written *before* each step; identity-checked undo |
| Storage | `storage/` | Capability probe per destination, volume identity, storage adapter (`LocalFS`, fault-injectable), catalog, saved locations + OS credential store |

**Why this stack.** Python has the most mature cross-platform tag library (`mutagen`) and direct
access to the OS primitives this tool depends on (`renameat2`, `renamex_np`, `MoveFileEx`, `fsync`,
`statvfs`). Qt 6 via PySide6 gives native, accessible widgets and a virtualized table on Windows,
macOS and Linux from one codebase; PyInstaller turns it into self-contained apps. SQLite (standard
library) holds the catalog, plans and the journal with crash-safe transactions.

Data locations: the catalog, plans and journal live in the per-user app-data folder
(`~/.local/share/DataCurator`, `~/Library/Application Support/DataCurator`,
`%LOCALAPPDATA%\DataCurator\DataCurator`), or `DATACURATOR_HOME` if set.

## Documentation

* [docs/TEMPLATES.md](docs/TEMPLATES.md) — template syntax, tokens, presets and examples
* [docs/COMPATIBILITY.md](docs/COMPATIBILITY.md) — filesystems, platforms and network shares
* [docs/VALIDATION.md](docs/VALIDATION.md) — what was tested, how, and the results (incl. benchmarks)
* [docs/LIMITATIONS.md](docs/LIMITATIONS.md) — what is not done or not verified
* [docs/PACKAGING.md](docs/PACKAGING.md) — building installers per platform

## Tests

```bash
python -m pytest                                  # unit, integration, simulated failures, GUI (offscreen)
sudo scripts/realfs_setup.sh                      # Linux: loop-mounted FAT32/exFAT/NTFS/XFS/ext4 + Samba
export DATACURATOR_REALFS=...                     # printed by the script
python -m pytest tests/test_realfs.py -v          # verified runs on those real filesystems
python scripts/benchmark.py                       # ~52,500-file library benchmark
```

Tests only ever use temporary fixtures; nothing touches a personal library.
