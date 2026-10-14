# Source inventory and destination checks — October 13, 2026

Version 1.3.2 adds source-relative category destinations and a complete desktop
inventory option. Saved profiles keep their explicit settings. Inventory includes
protected game/application files but planning leaves them in place. A preview
lists all detected records; inaccessible folders produce a visible incomplete-scan
notice and scan details.

150 tests pass, with 11 existing environment-dependent tests skipped. New checks
cover mixed Downloads contents, unknown and missing extensions, hidden files and
folders, nested files, existing destinations, protected applications/games,
multiple sources, profile save/load, custom destinations, partial permission
errors, invalid relative paths, category-folder links outside a source, preview
without writes, apply/rescan and undo. A rescanned document uses its category root
as the project base, preventing repeated Documents/Documents nesting. Explicit
category folders take precedence over loose album-art guesses for photos.

A read-only inventory check against the actual Downloads directory detects
17,609 files in 4,479 folders with zero scan errors. All 69 eligible regular files
directly in Downloads appear in the catalog. There are 281 skipped links. Metadata
extraction is disabled for this visibility diagnostic; actual metadata extraction
and full workflows are exercised on generated test media. No Downloads files are
moved, renamed or written. All diagnostic catalog data is stored in the task workspace.

Qt interface checks use the offscreen platform. Live macOS privacy authorization
is separate from command-line folder visibility; scan errors now expose the
affected path and point to macOS Files and Folders permission settings.

The packaged CLI passes all-file inventory, two-source apply/rescan/undo, HEIC/PDF
and the existing organization/duplicate checks. Bundle metadata and deep code
signature verification pass. Offscreen packaged GUI startup succeeds. Native
desktop launch cannot be verified in this sandbox: LaunchServices reports a
missing executable for both this build and the previously published 1.3.1 app,
and a direct Cocoa launch cannot connect to macOS window/pasteboard services.
