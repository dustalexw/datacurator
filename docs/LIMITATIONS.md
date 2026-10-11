# Known limitations

Everything listed in the README and TEMPLATES is implemented and covered by tests. The items below are
either not implemented or implemented but not verified in this release.

## Not verified on these platforms

* **Windows was not run.** Its code paths exist (volume information and long-path detection,
  `MoveFileExW` no-replace renames, UNC/mapped-drive detection, `net use`) but have not been executed.
  Treat Windows as beta until the test suite has been run there (`python -m pytest`).
* **macOS was verified on CI only.** The full test suite passes on GitHub-hosted macOS 14 (Apple
  silicon) and macOS 15 (Intel) runners on APFS, but the app was not used on a physical Mac display,
  with network shares, or with Finder reconnection.
* Kernel CIFS and NFS clients, Btrfs, ZFS, HFS+, ReFS: expected to work, not exercised.
* The GUI was exercised with the offscreen Qt platform on Linux (functional tests and screenshots),
  not on a physical display, and not with a screen reader. Widgets carry accessible names and the
  workflow is keyboard-operable, but no formal accessibility audit was done.
* Standalone builds: Linux (smoke-tested here) and macOS arm64/x86_64 (built and smoke-tested by `.github/workflows/macos.yml`). macOS builds are ad-hoc signed, not notarized; no Windows build yet.

## Not implemented

* **Live filesystem watching.** There is no watcher; re-scanning is incremental and fast
  (52,500 unchanged files in 1.6 s), and unattended runs are meant to be scheduled with the OS
  scheduler (`curator run`).
* **Parallel transfers.** Files are moved one at a time (metadata reading is parallel and bounded).
  The `network.max_concurrent_transfers` profile key is reserved and currently fixed at 1.
* **Copy mode** (copy into the library and keep the originals). Moving out of read-only media such as
  optical discs is therefore refused; those sources can be scanned and previewed only.
* **Online music metadata** (e.g. MusicBrainz) — music is organized from embedded tags, overrides and
  folder-name suggestions only. Optional video providers are **TVmaze** and **TMDB**; their response handling is tested with recorded responses. TMDB requires a user-supplied API Read Access Token.
* **Alternate episode orders** (DVD/absolute ↔ aired mapping). Episode numbers in filenames are used
  as given; absolute numbering must be chosen explicitly and produces `Series - 123` names without
  mapping to seasons.
* **Per-file collision choices in the GUI.** Collision handling is a profile setting (review / numbered
  name / skip); individual files can be excluded or given different values, but not individually
  renamed with a number from the review table.
* Tag writing covers title, artist, album, album artist, date, genre, track and disc for formats that
  mutagen's "easy" interface supports (MP3, FLAC, M4A, Ogg Vorbis, Opus). WAV/AIFF/WMA tag writing
  fails safely (file unchanged, reported).
* Embedded video tags are read from MP4/M4V/MOV only; MKV/AVI use filenames and NFO sidecars.
* Game-folder protection is a heuristic, not a complete game database. Add unrecognized games/save folders to Exclusions.
* Duplicate finding compares exact bytes, omits empty files and leaves hard links alone.
* Duplicate removal moves extra copies to a quarantine folder (undoable); emptying the quarantine is
  left to the user.
* Undo works per run; undoing an undo is not supported (preview and apply again instead).
* Most RAW photos still need extra readers and go to review unless the modified-time fallback is
  enabled. HEIC/HEIF capture dates are supported through the bundled `pillow-heif` reader.
  Photos without a capture date stay in review unless you set a year/month or enable the
  modified-time fallback in Rules → Photos.

## Behaviour to be aware of

* **Preservation across volumes.** Renames on the same volume keep everything. Copies to another
  volume keep content, modification/access times and permission bits (and extended attributes on
  Linux where both sides support them), but not ACLs, NTFS alternate data streams, creation time,
  Finder metadata or hard-link relationships. These are listed per file before applying and can be
  blocked with *Block transfers that cannot preserve attributes*.
* **Check-then-rename fallback.** Filesystems without `renameat2`/`renamex_np` and without hard links
  (FAT, exFAT and SMB through FUSE clients) use a check followed by a rename. Another program creating
  the same name in that instant could be overwritten by the OS rename. All other platforms/filesystems
  use atomic no-replace primitives.
* **Hung network mounts.** Reachability checks use timeouts so the app does not freeze, but a file
  operation already blocked inside the kernel on a stale *hard* NFS mount cannot be interrupted until
  the mount responds or is unmounted (`umount -l`).
* **Network identity.** Some network clients renumber inodes after reconnecting. For shares, identity
  is the share source and filesystem type; resuming additionally requires that files written earlier
  in the run are still present unchanged.
* **In-place previews on FUSE volumes.** Previews never write into source folders, so the behaviour of
  a FUSE filesystem being organized in place is inferred until Apply measures it; if it differs, Apply
  stops and asks for a new preview (which then uses the measured behaviour). Running **Check** on the
  destination first avoids the extra step.
* Existing folders are reused when they differ only by case or Unicode form (an existing `radiohead/`
  receives "Radiohead" albums) so the library never forks into near-duplicate folders.
* Very large libraries: memory grew to 187 MB for 52,500 files; libraries above ~1 million files were
  not tested.
