# Validation report

Build 1.0.0 · tested 10 October 2026 · Linux 6.18.44 x86_64 (virtual machine: 2 × Intel Xeon @ 2.80 GHz,
7.8 GB RAM, ext4 virtual disk) · Python 3.13.16 · PySide6 6.12 · mutagen 1.48.1

## Version 1.3.0 update — 11 October 2026

Local macOS test suite: **136 passed, 11 skipped**. The skipped cases require Linux mounts or
filesystems unavailable on this machine. New coverage includes extension routing and validation,
in-source idempotence and undo, format folders with media companions, game/manual/realpath exclusions,
filtered duplicates with preferred keepers, selected quarantine and undo, changed keepers before
and after preflight, cancellation, hard links, NFO/embedded MP4 identification, local metadata
precedence, bounded/cached TMDB responses, ambiguity resolution, and the desktop controls.

Packaged CLI checks exercise HEIC/PDF and the new extension/game/NFO/duplicate workflows with
exact-byte moves and undo. macOS CI runs the suite and packages the app for Apple silicon and Intel,
including native activation verification. Provider behavior uses recorded responses; no live TMDB
account or token is needed for tests. GUI tests run through Qt's offscreen platform.

The original validation report below records the 1.0.0 Linux filesystem work.

## Result

**106 tests passed, 0 failed** (`python -m pytest` with the real-filesystem environment from
`scripts/realfs_setup.sh`). Without that environment: 97 passed, 3 skipped (the real-filesystem cases).

| Suite | Tests | What it covers |
|---|---|---|
| `test_naming.py` | 12 | Template engine (optional sections, padding, fallbacks, clean punctuation, required vs optional folders, Season 00), sanitization (reserved names, forbidden characters, trailing dots, NFC, length truncation at code points), equivalence keys |
| `test_stage1_plan.py` | 11 | Preview performs no source mutation (byte + timestamp snapshot), all audio formats, presets, determinism, incremental scan, documents/photos, minimal folders, organize-in-place, already-organized = no change, sort-only / rename-only, sanitization in preview, symlink loops |
| `test_stage2_execute.py` | 24 | Apply + idempotence, never-overwrite (planned and raced), alternate-name policy, hash duplicates, rename cycle, case-only rename, cross-volume verified copy, interrupted copy (resume and clean rollback), crash after finalize, disk full, source modified during copy, change after scan, transient retries/backoff, unavailable share → interrupted → resume, different storage at the same path, path escape, destination symlink escape, cancellation, undo, undo conflicts, empty-folder cleanup, missing destination root |
| `test_stage3_music.py` | 18 | Conflicting album artists, featured artists, compilations (flag, "VA", configurable name, Artist mode), untagged multi-artist albums + override, multidisc (one folder, CD folders with "(Disc 1)" titles, disc-folder preset), single disc, missing track numbers/titles, singles, releases with identical titles, ambiguous releases, multiple audio versions, unsupported metadata, companions + lyrics, cue rewrite + undo, single-file image + cue, explicit-only tag writing + undo, existing folder spelling reuse |
| `test_stage3_tv.py` | 15 | S01E02, 1x02, S01E01E02, S01E01-E03, specials, daily shows, absolute numbering convention, subtitles/audio/nfo/thumb suffixes, `Subs/` folders and episode-code matching, folder-derived series, spelling unification, ambiguous series year, episode folders, rename/sort-only, companion conflict holds video, `.nfo` metadata, TV idempotence, movies, provider titles + cache, provider ambiguity choice, provider failure fallback |
| `test_edge_cases.py` | 12 | Unicode collisions (NFC/NFD/case), reserved names in generated paths, long names/paths, apply refuses when destination behaves differently, lossy-transfer blocking, dedupe approval + undo, unattended profile gate, profile validation, example profiles, catalog tags/collections, reports, live probe |
| `test_cli.py` | 3 | Every documented CLI command: init-profile, scan, plan (+CSV/JSON), show, explain, accept, apply, history, undo (dry run and real), recover, probe, presets, template, override, exclude/include, tag, collection, search, locations, dedupe (approval gate), report, unattended `run` gate |
| `test_gui.py` | 2 | Offscreen Qt: full workflow (preview → review queue → accept suggestion → search → tree → apply → undo), paged table with 1,500 rows |
| `test_realfs.py` | 9 | **Real filesystems** (below) |

## Required scenarios → tests

| Scenario | Test(s) | Kind |
|---|---|---|
| Conflicting metadata | `test_conflicting_album_artist_goes_to_review_with_suggestion` | logic |
| Compilation albums | `test_compilations_kept_together_with_configurable_name`, `test_untagged_multi_artist_album_needs_review_then_override_fixes` | logic |
| Multidisc releases | `test_multidisc_same_folder_and_disc_folders`, `test_multidisc_split_across_cd_folders_with_disc_in_album_title`, `test_single_disc_never_gets_disc_folder` | logic |
| TV specials | `test_patterns_specials_multi_episode` | logic |
| Multi-episode files | `test_patterns_specials_multi_episode` | logic |
| Subtitle associations | `test_subtitles_audio_and_metadata_follow_with_suffixes`, `test_subs_folder_and_episode_code_matching`, real FS cases | logic + real FS |
| Unicode collisions | `test_unicode_equivalent_names_share_one_folder_and_collide`, `test_equivalence_key_case_and_unicode` | logic |
| Reserved names | `test_reserved_names_and_trailing_dots_in_generated_paths`, `test_reserved_names_and_prohibited_chars_portable` | logic |
| Case-only renames | `test_case_only_rename`; real FS cases on 7 filesystems | real FS |
| Rename cycles | `test_rename_cycle_swap`; real FS cases on 7 filesystems | real FS |
| Long paths | `test_long_names_and_paths_are_shortened_or_reviewed` | simulated limit |
| Concurrent file changes | `test_change_after_scan_blocks_item`, `test_source_modified_during_copy_is_kept`, `test_file_appearing_after_preview_is_not_overwritten` | real files, injected timing |
| Symlink loops | `test_symlink_loop_is_detected` | real symlinks |
| Cross-volume transfers | `test_cross_volume_verified_copy` (ext4 → tmpfs); real FS cases | real FS |
| Interrupted operations | `test_interrupted_copy_recovers_without_losing_source`, `..._without_resume_restores_clean_state`, `test_crash_after_finalize_before_source_removal`, `test_cancel_between_items` | **simulated crash** |
| Unavailable shares | `test_unavailable_share_interrupts_then_resumes` (**simulated** `ENOTCONN`); `test_real_share_disconnect_resume_and_undo` (**real** Samba server killed) | simulated + real |
| Undo conflicts | `test_undo_conflicts_are_reported_not_forced` | real files |

## Simulated vs verified

* **Simulated storage failures** use `FaultFS`, a subclass of the real storage adapter that injects
  `ENOSPC`, `EBUSY`/`EAGAIN`, `ENOTCONN` and a `BaseException` standing in for power loss or a killed
  process at a chosen byte offset or step. The data operations around the fault are real.
* **Verified on real filesystems** (`tests/test_realfs.py`, results in `docs/realfs-results.json`):

| Target | Driver | Measured by probe | Result |
|---|---|---|---|
| ext4 (loop image) | kernel | case-sensitive, xattr, links | pass |
| XFS (loop image) | kernel | case-sensitive, xattr, links | pass |
| tmpfs | kernel | case-sensitive, xattr, links | pass |
| NTFS (loop image) | ntfs-3g (FUSE) | case-sensitive*, xattr, links | pass |
| FAT32 (loop image) | fusefat (FUSE) | case-insensitive, Windows rules, **ASCII-only names** | pass (names transliterated) |
| exFAT (loop device) | exfat-fuse | case-insensitive, Windows rules | pass |
| SMB share | Samba 4.19 + smbnetfs (FUSE) | case-insensitive, Windows rules, network | pass |
| SMB server killed mid-batch | same | — | pass: interrupted → resumed → identical hashes |
| squashfs | kernel, read-only | read-only | pass (scan/preview; moves refused) |

\* ntfs-3g on Linux is case-sensitive unless mounted with `windows_names`; the portable preset still
produces Windows-valid names.

Each filesystem case: cross-volume organize of music (FLAC/MP3 with Unicode and reserved characters),
artwork, a 5 MB video and a `.en.forced.srt` from the system disk; every copied file's content hash
compared with the original; the destination re-planned in place with **zero** proposed changes; a
case-only rename and a two-file rename cycle performed on that filesystem; both runs undone and the
original hashes confirmed.

**Not verified here:** Windows and macOS (no such machines in this environment), kernel CIFS and NFS
clients, Btrfs and ZFS (modules unavailable), APFS/HFS+/ReFS. See [COMPATIBILITY.md](COMPATIBILITY.md).

## Problems found by real-filesystem testing (and fixed)

1. FUSE clients on case-insensitive volumes kept a stale cached alias after a rename, so a case-only
   rename looked like a collision → existence is now confirmed against the directory listing.
2. Inode numbers on FUSE FAT/exFAT/SMB are not stable → same-entry detection falls back to a listing
   check; network identity no longer relies on inode numbers (a resume instead proves the storage still
   holds the files the run already wrote).
3. The userspace FAT driver rejects non-ASCII names → new *ASCII-only* capability; names are
   transliterated and the change is shown in the preview.
4. Extended-attribute calls wedge some SMB FUSE clients → xattrs are never touched on network, FAT or
   exFAT filesystems.
5. A FUSE SMB client reports 0 bytes free → treated as "unknown" instead of "full".
6. Destination roots that did not yet exist produced an identity mismatch at apply time → identity is
   anchored to the existing folder it was read from.

## Benchmarks (measured)

Hardware as above; `python scripts/benchmark.py`. Raw output: `docs/benchmark-*.json`.

**Same volume, 52,500 files** (30,000 tagged FLAC tracks in 2,500 albums with cover art, 10,000
episodes + 5,000 subtitles, 5,000 documents):

| Step | Time | Rate |
|---|---|---|
| Cold scan (32,500 metadata reads) | 23.0 s | 2,281 files/s |
| Incremental rescan (nothing changed) | 1.6 s | — |
| Plan | 14.3 s | 52,500 changes, 0 review |
| Apply (journaled renames, fsync per step) | 72.0 s | 729 files/s |
| Re-plan organized library in place | 10.4 s | **0 changes** |
| Undo | 79.9 s | 52,500 restored, complete |
| Peak memory (whole run) | 187 MB RSS | |

**Cross volume** (ext4 → tmpfs), 1,250 files, 1.68 GB, 400 × 4 MB videos: apply 15.4 s
(≈104 MB/s including the source hash during copy, a re-read of the copy and a final hash after
finalizing), undo 20.0 s (verified copies back), re-plan 0 changes.

Durability costs time: every step is committed to the journal with `synchronous=FULL` and directories
are fsynced. Real-disk numbers will differ from this virtual disk.

## Acceptance criteria

| Criterion | Evidence |
|---|---|
| Preview performs no source mutations | `test_preview_does_not_mutate_sources` (byte + mtime snapshot of files and folders) |
| Repeating an applied plan produces no unnecessary changes | `test_apply_moves_and_is_idempotent`, `test_tv_apply_and_rerun_is_stable`, real FS re-plans, benchmark re-plan = 0 |
| Ambiguous files remain available for review | music/TV review tests; review queue filter in GUI test |
| Existing destination files are never silently overwritten | `test_existing_destination_never_overwritten`, `test_file_appearing_after_preview_is_not_overwritten`, no-replace rename primitives |
| Verified copies retain identical content | cross-volume tests and real FS hash comparisons |
| Recovery does not silently discard source files | interrupted-copy, crash-after-finalize, disk-full, SMB-kill tests |
| Music follows the selected Artist–Album rules | `test_stage3_music.py`, presets test |
| Television follows the selected Series–Season rules | `test_stage3_tv.py` |
| Generated folders remain minimal and deterministic | `test_documents_photos_and_minimal_folders`, `test_single_disc_never_gets_disc_folder`, determinism test |
| Large-library benchmarks report measured performance and hardware | above |
| Every claimed feature works; unfinished features identified | this report + [LIMITATIONS.md](LIMITATIONS.md) |
