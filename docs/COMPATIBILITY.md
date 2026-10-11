# Compatibility matrix

Data Curator works through the operating system's normal file APIs, so it can use any filesystem the
OS can mount. It does **not** add filesystem drivers: where an OS needs a third-party driver to write
a filesystem, that is stated below rather than claimed as native support.

Behaviour is measured **per destination** (case sensitivity, Unicode normalization, Windows naming
rules, ASCII-only drivers, link/xattr support, free space, read-only state) by creating and removing a
hidden probe file — never assumed from the filesystem's name. A preview never writes into a source
folder; if a destination inside a source behaves differently from what was assumed, Apply stops and
asks for a new preview, which then uses the measured behaviour.

## Legend

* **Verified** — exercised end to end by the automated tests in `tests/test_realfs.py` on a real mounted
  filesystem (cross-volume organize with hash comparison, idempotent re-plan, case-only rename, rename
  cycle, undo). Environment: Linux 6.18 x86_64, Python 3.13, October 2026.
* **Expected** — the code path exists and the same primitives are used, but it was not run on that
  platform/filesystem for this release.
* **Read-only** — can be scanned, cataloged and previewed; not modified.
* **Unavailable** — not supported without additional software.

## Filesystems

| Filesystem | Linux | Windows | macOS | Notes |
|---|---|---|---|---|
| ext4 | **Verified** | Unavailable (needs WSL/driver) | Unavailable (needs driver) | Case-sensitive; probe confirms |
| XFS | **Verified** | Unavailable | Unavailable | |
| tmpfs | **Verified** | — | — | Used as the second volume for cross-volume tests |
| ext2/ext3 | Expected | Unavailable | Unavailable | Same kernel driver family as ext4 |
| Btrfs | Expected (kernel module absent in test VM) | Unavailable | Unavailable | Subvolumes have different device IDs: moves between them are verified copies (EXDEV fallback) |
| ZFS | Expected | Unavailable (OpenZFS port not tested) | Unavailable | Case sensitivity/normalization are dataset properties; the probe measures them |
| NTFS | **Verified** via ntfs-3g (FUSE) | Expected (native) | Read-only natively; write needs a third-party driver | ntfs-3g allows names Windows cannot open — the default *portable* preset avoids them |
| ReFS | — | Expected | — | |
| FAT32 | **Verified** via fusefat (FUSE); kernel `vfat` expected | Expected | Expected | 4 GiB file limit enforced in preview; 2-second timestamps; no permissions/links. The userspace fusefat driver rejects non-ASCII names: detected by the probe, names are transliterated and shown in the preview |
| exFAT | **Verified** via exfat-fuse; kernel `exfat` expected | Expected | Expected | Case-insensitive; no permissions/links/xattrs |
| APFS | — | Unavailable | Expected | Case-insensitive and normalization-insensitive by default (probe measures; case-sensitive APFS also handled) |
| HFS+ | Unavailable (read-only kernel driver for journaled HFS+) | Unavailable | Expected | Stores NFD names; equivalence keys treat NFC/NFD as one name |
| ISO 9660 / UDF (optical) | Read-only (expected) | Read-only (expected) | Read-only (expected) | |
| squashfs | **Read-only, verified** | — | — | Scan and preview work; moving out is refused because originals cannot be removed; as a destination files go to review |

## Network storage

| Scenario | Status | Notes |
|---|---|---|
| SMB share mounted on Linux | **Verified** (Samba 4.19 server, smbnetfs FUSE client) | Includes a **real server kill mid-batch**: the run stops as *interrupted*, nothing is lost, Resume completes after the server returns, content hashes match, undo restores everything |
| SMB via kernel CIFS (`mount.cifs`) | Expected | The test VM kernel has no CIFS module |
| NFS v3/v4 mounts | Expected | Hard-link fallback gives atomic no-replace renames on NFS |
| Windows UNC paths (`\\server\share`) and mapped drives | Expected | Detected as network via `GetDriveTypeW`; reconnection with `net use` |
| macOS mounted shares (`/Volumes/...`) | Expected | Reconnection through Finder (`open smb://…`, Keychain) |
| NAS over SMB/NFS | Same as the rows above | |

Network behaviour common to all shares: fewer concurrent metadata reads (2 instead of 4), one transfer
at a time, transient errors retried with bounded exponential backoff (0.5 s → 8 s, 4 retries by
default), unreachable or stale mounts detected with a timeout instead of hanging the UI thread, and
the run marked *interrupted* with reconnection guidance. Before resuming, the destination must still
contain the files this run already wrote (size and time), so a reconnected path cannot silently point
at different storage. Saved locations record the share's identity; credentials go to the OS credential
store through `keyring` (Windows Credential Manager, macOS Keychain, Secret Service).

## Feature handling

| Concern | How it is handled | Status |
|---|---|---|
| Case-only renames (`song.flac` → `Song.flac`) | Via a temporary name in the same folder; works on case-insensitive volumes and FUSE clients with stale name caches | **Verified** on ext4, XFS, tmpfs, NTFS-3G, FAT32, exFAT, SMB |
| Rename cycles / swaps | Occupied sources are staged to temporary names first | **Verified** (same filesystems) |
| Unicode equivalent names (NFC/NFD, case) | Names generated in NFC; equivalence keys fold case and normalization so variants never fork a folder or collide silently | Verified (unit + real FS) |
| Reserved names, forbidden characters, trailing spaces/periods | Sanitized with every change listed in the preview | Verified |
| Name and path length | 255 bytes / 255 UTF-16 units; full-path limit (260 on Windows unless long paths are enabled); filenames shortened at a code-point boundary or sent to review | Verified (simulated limits); Windows limit expected |
| File size limits | FAT32 4 GiB enforced at preview | Verified (logic); not exercised with a >4 GiB file |
| Free space | Checked per destination before applying; unknown free space (some SMB clients) is reported and disk-full is handled while copying | Verified |
| Same-volume moves | Atomic no-replace rename: `renameat2(RENAME_NOREPLACE)` (Linux), `renamex_np(RENAME_EXCL)` (macOS), `MoveFileExW` without replace (Windows); fallback `link`+`unlink`; last resort check-then-rename (documented race) | Linux verified; macOS/Windows expected |
| Cross-volume moves | Copy to `.curator-part-…` in the destination folder → fsync → BLAKE2b verify → copy times/mode → no-replace rename → verify → remove source only if unchanged | Verified |
| Timestamps / permission bits | Preserved (renames keep everything; copies copy mtime/atime and mode) | Verified on Linux |
| Extended attributes | Copied on Linux when both sides support them; skipped on network/FAT/exFAT (some SMB FUSE clients fail on xattr calls) | Verified |
| ACLs, NTFS alternate data streams, creation (birth) time, Finder metadata | Kept by renames; **not** copied across volumes — disclosed per file before applying, and *Block transfers that cannot preserve attributes* skips them | Disclosure verified |
| Ownership | Kept by renames; across volumes files belong to the current user (unless running as root) — disclosed | Verified |
| Hard links | Renames keep links; cross-volume copies make an independent file — disclosed and blockable | Verified |
| Symlinks / junctions | File symlinks are never organized. Folder symlinks (and Windows junctions) are followed only when enabled, with cycle detection; destinations reached through a symlink that leaves the destination root are refused | Verified (Linux); junctions expected |
| Mount boundaries | Not crossed while scanning unless enabled | Verified |
| Read-only volumes | Detected (mount flags or probe); scanned only | Verified (squashfs) |
| Volume identity | Filesystem type, folder inode, `statvfs` fsid (local) or share source (network), volume serial (Windows) | Verified on Linux |

## Platforms

| Platform | Status |
|---|---|
| Linux x86_64 (Ubuntu 24.04 userland, kernel 6.18) | **Verified**: full test suite, GUI (offscreen Qt), CLI, PyInstaller build runs |
| Windows 10/11 | Expected: Windows-specific code paths written (volume info, long paths, MoveFileEx, `net use`) but not executed in this environment |
| macOS 11+ | Expected: `renamex_np`, APFS/HFS+ probing, Finder reconnection written but not executed |
