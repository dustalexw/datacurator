# Data Curator — architecture, UX and performance audit

Scope: whole repository at `4738e8c` (v1.4.1, ~18k lines of Python, 17 test modules).
Method: read `cli.py`, `engine/{api,executor,scanner,journal,util}.py`, `storage/fsops.py`,
`models.py`, test setup and CI; targeted greps for the rest. **Not done:** the existing test suite
and the GUI were not run (no pytest/PySide6/mutagen in the audit environment; auto mode blocked
installing them). New code was syntax-checked and smoke-tested on Python 3.12 only. Findings about
`planner.py` (1.6k lines) and `gui/app.py` (1.6k lines) come from structure/greps, not a line-by-line read.

The honest headline: this is already a carefully engineered tool — journaled, crash-recoverable,
no-replace moves, hash-verified cross-volume copies, capability probing, a `LocalFS` seam. The work
is in the seams around that core, not the core's safety model, which I deliberately left alone.

---

## Phase 1 — Findings

### 1.1 Architecture

| # | Finding | Evidence | Severity |
|---|---|---|---|
| A1 | **Layering inversion** (storage imported engine) | `storage/fsops.py` imported `engine.util.fsync_dir`, while `engine/executor.py` imports `storage.fsops` — a package cycle. | Medium — **fixed** |
| A2 | **Three incompatible cancellation conventions** | `scanner.Cancelled`, `executor.Cancelled`, and `InterruptedError` (planner, `hash_file`). The CLI caught none of them, so Ctrl-C during `plan` printed a traceback. | High — **fixed at the CLI boundary**; unify in engine (roadmap) |
| A3 | **Two incompatible progress callback shapes** | scan/plan: `(folder:str, n:int)`; apply: `dict`. The CLI renderer branched on `len(args)`. | Medium — **fixed** (`core.progress`) |
| A4 | **Monoliths** | `planner.py` 1607 lines/58 defs in one `Planner` class; `gui/app.py` 1577; `executor.py` 934 (preflight + run + staging + copy + recovery + reference rewriting in one module). | Medium — roadmap |
| A5 | **Stringly-typed profile** | The profile is a nested `dict[str, Any]` merged/normalized in `profiles.py` and indexed by string keys everywhere (`profile["dedupe"]["quarantine_dir"]`). No static checking; typos fail at runtime. | Medium — roadmap |
| A6 | **Business flow in the CLI** | `cmd_run` (recover → scan → plan → apply policy), `cmd_accept`, `cmd_locations` contain orchestration the GUI must re-implement. | Medium — roadmap (`Engine.run_unattended`) |
| A7 | **UI helper lives in the engine** | CLI imports `fmt_bytes` from `engine.executor`. | Low — **fixed** (`core.progress.format_bytes`) |
| A8 | **No logging at all** | `grep "import logging"` → nothing. Diagnostics are `print` or swallowed `except` blocks (≈20 broad excepts, 4 in `capabilities.py`, 4 in `metadata/other.py`). | High — **fixed** (`core.logs`) |

Dependency direction after the fixes: `models ← core ← storage ← engine ← {cli, gui}`; `metadata`,
`naming` are leaves. `engine`/`storage` import no UI code.

### 1.2 UX / workflow

* **No SIGTERM/SIGHUP handling.** Only SIGINT during `apply`; `kill`, systemd stop, terminal close and
  Windows Ctrl-Break bypassed the cooperative cancel (the journal still made it recoverable, but the
  user saw an abrupt death). **Fixed** (`core.lifecycle`).
* **Raw tracebacks for ordinary failures.** `main()` caught only `CuratorError`/`FileNotFoundError`.
  A `PermissionError`, locked/corrupt SQLite file, bad profile JSON, ENOSPC or `curator override`
  with no path (→ `os.path.abspath(None)` `TypeError`) produced a stack trace. **Fixed** (`core.errors`).
* **Exit codes collide with argparse:** usage errors exit 2, the same as "finished with errors" (`PARTIAL`).
  Scripts can't tell them apart. Documented in `ExitCode`; changing argparse's code needs a custom
  `ArgumentParser.error` (roadmap — it is a behaviour change).
* **Progress:** one line per phase, no ETA, no throughput, no total in scan, spams a `\r` line when piped to
  a file/CI. **Fixed**: TTY-aware live line with smoothed ETA, sparse plain lines when not a TTY, optional JSON lines.
* **Silent truncation:** scan errors were capped at 20 with no hint more existed. **Fixed.**
* **`plan` re-parsed the profile and ignored the scan result:** if the scan was cancelled or errored it
  went on to plan against a partial catalog. **Fixed** (cancelled scan now stops with exit 130).
* **Discoverability:** 21 sub-commands with no grouping, no shell completion, no `curator doctor`, and the
  first-run path requires knowing to run `init-profile` first (`--profile is required` is the only hint).
  Roadmap (see Phase 3).

### 1.3 Performance and concurrency

| # | Finding | Impact |
|---|---|---|
| P1 | **`os.path.realpath(d)` evaluated per file** in `_scan_root` (`via_symlink=d != realpath(d) and follow_symlinks` — the cheap boolean was last, so the expensive call always ran). | Several syscalls × every file; dominant cost on warm-cache rescans of large trees, worse on network mounts. **Fixed:** once per directory, and only if `follow_symlinks`. |
| P2 | **Quadratic resume.** `Executor.partial_for` loaded every journal op of the batch (`Journal.ops`, then filtered in Python) *for each cross-volume file* on resume. | 100k-file resume ≈ 10¹⁰ row visits. **Fixed:** one indexed SQL lookup (`Journal.kept_partial`). |
| P3 | **Scanner parallelism barrier per directory.** Metadata futures are collected and joined at the end of every directory, so the pool idles on trees of many small folders. | Throughput limited by directory fan-out, not workers. Roadmap: stream futures across directories with a bounded window (`engine.batch.run_resilient` is the building block). |
| P4 | **Redundant verification reads on cross-volume copy.** Source read once while copying (hash h), then `hash_file(tmp)` re-reads the copy, then `finish_source_removal` re-reads `dst` after an atomic rename that cannot change content. | ~3× read traffic of the destination. The first verify is the valuable one; the second is defence-in-depth. Make the post-rename hash optional (`--paranoid`), keep default off. Not changed: it is part of the safety contract and needs a decision from you. |
| P5 | **Serial copy loop**: read → write → hash on one thread, 1 MiB buffer. | Underuses fast NVMe↔NAS links. Roadmap: 4–8 MiB buffer + hash on a worker thread (blake2b releases the GIL). |
| P6 | **Per-file journal commits at `synchronous=FULL`**: same-volume rename = 3 INSERT/UPDATE commits + 2 directory fsyncs. | Correct but expensive for 100k+ renames. Roadmap: group commit (write intent for N renames in one transaction, execute, mark N done) — recovery already handles "started" ops idempotently, so intent batching is compatible. |
| P7 | **Whole plan materialized.** `Executor._items()` builds a list of every `PlanItem` (and `_moved` grows unbounded). | Memory ∝ files; fine at 100k, heavy at millions. Stream via cursor. |
| P8 | `os.stat(full)` per subdirectory just for the mount check, though the directory is `stat`ed again when popped. | One redundant stat per directory. Fixed in the new `storage.walk` (single stat); scanner not yet migrated. |

### 1.4 Resilience

* **Silent failure paths:** `DirListingCache.listing` swallows *all* `OSError` into `None`, so a permission
  error on a destination folder is indistinguishable from "folder doesn't exist" (the planner then
  plans to create it; the executor surfaces the real error late). `_unstage`, `_safe_unlink` and
  `_cleanup_dirs` swallow `OSError` by design but leave nothing in the log.
* **Signal-unsafe cleanup:** SIGINT handler was installed only inside `_apply` and restored in `finally` —
  `scan`/`plan` ran with the default handler, so a mid-scan Ctrl-C raised `KeyboardInterrupt` deep inside a
  `ThreadPoolExecutor` `with`/SQLite transaction.
* **Platform notes:** `renameat2`/`renamex_np` fast paths are good; the `link+unlink` and `checked-rename`
  fallbacks are honestly documented as having a race window. `reconnect_hint` uses `os.uname()`, which does
  not exist on Windows (reached only when `os.name != "nt"`, so safe). **CI covers macOS only** although the
  code carries Windows/Linux branches (`winerror` handling, `renameat2`, xattrs) — those paths are
  effectively untested in CI (`tests/test_realfs.py` needs root loop mounts).
* **Dependency footprint:** `keyring`, `pillow-heif`, `psutil` are hard dependencies though only some
  workflows need them (credentials, HEIC, capability probing). Consider extras so a headless NAS install is small.
* **No static analysis in CI** (no ruff/mypy), despite widespread type hints.

---

## Phase 2 — Target structure

Delivered pieces are marked ✅; the rest is the proposed move (mechanical, do it one package per PR).

```
src/datacurator/
├── models.py                 # pure dataclasses/enums (unchanged)
├── core/                     # ✅ UI-agnostic runtime services (stdlib only)
│   ├── errors.py             # ✅ ActionableError(why/fix), ExitCode, explain_exception
│   ├── lifecycle.py          # ✅ ShutdownCoordinator (SIGINT/SIGTERM/SIGHUP/SIGBREAK)
│   ├── progress.py           # ✅ ProgressEvent, TerminalProgress, JsonLinesProgress, legacy adapter
│   ├── logs.py               # ✅ rotating JSON file log + quiet console
│   ├── paths.py              #    config/data/cache/log dirs (platformdirs) — one place
│   └── config.py             #    typed Profile (dataclasses/TypedDict) + schema version + migration
├── storage/                  # platform & filesystem abstraction
│   ├── fsops.py              # ✅ LocalFS (+ fsync_dir moved here)
│   ├── walk.py               # ✅ safe deterministic traversal with issue reporting
│   ├── capabilities.py  catalog.py  locations.py
├── engine/                   # domain logic; no UI imports
│   ├── api.py                #    Engine facade (add run_unattended(), context-manager close)
│   ├── batch.py              # ✅ bounded-concurrency, retrying, cancellable batch runner
│   ├── scan/                 #    scanner.py + exclusions.py
│   ├── plan/                 #    planner.py split: music.py tv.py movies.py documents.py photos.py collisions.py
│   ├── apply/                #    preflight.py  executor.py  copy.py  recovery.py  undo.py  journal.py
│   └── dedupe.py  filesets.py  planstore.py
├── metadata/  naming/        # leaves (unchanged)
├── cli/                      # thin: parser.py, commands/{scan,plan,apply,history,catalog,...}.py, render.py
└── gui/                      # PySide6; talks to Engine through a Worker only
```

**Boundaries (enforce with `import-linter` in CI):** `core` imports nothing internal except `models`;
`storage` may import `core`; `engine` may import `storage`, `core`, `metadata`, `naming`; `cli` and `gui`
may import `engine` and `core`; nothing imports `cli` or `gui`. Headless use is then
`Engine(...).scan/plan/apply` plus a `ProgressSink` and a `ShutdownCoordinator` — exactly what `cli.main` does now.

**Configuration & state layout** (all via `platformdirs`; `DATACURATOR_HOME` / `DATACURATOR_LOG_DIR` override):

| Kind | Content | macOS | Linux | Windows |
|---|---|---|---|---|
| config | `profiles/*.json` (schema-versioned), `settings.json` | `~/Library/Application Support/DataCurator` | `~/.config/DataCurator` | `%APPDATA%\DataCurator` |
| data | `catalog.sqlite`, `journal.sqlite`, `journal-backups/`, `plans/` | same as config | `~/.local/share/DataCurator` | `%LOCALAPPDATA%\DataCurator` |
| cache | provider (TMDB/TVmaze) responses | `~/Library/Caches/DataCurator` | `~/.cache/DataCurator` | `%LOCALAPPDATA%\DataCurator\Cache` |
| logs | `datacurator.log` (JSON lines, 5×2 MB) | `~/Library/Logs/DataCurator` | `~/.local/state/DataCurator/log` | `%LOCALAPPDATA%\DataCurator\Logs` |

Today config, data and the catalog share one directory and the cache lives inside the catalog; separating them
lets users back up the journal and wipe caches safely. Profiles should carry `"schema": 1` so future
renames migrate instead of silently normalizing.

---

## Phase 3 — UX transformation

**First run (`curator` with no arguments / `curator start`).** Detect, then ask only what can't be detected:
1. `doctor` checks: Python/OS, optional deps present (HEIC, keyring, ffprobe), data dir writable, and *probes each
   candidate folder* with the existing `storage.capabilities.probe` (case sensitivity, free space, network).
2. Suggest sources from `~/Downloads`, `~/Music`, `~/Movies`, `~/Pictures`, mounted volumes; user picks by number.
3. Offer 3 presets (Music, Mixed files, Photos & documents — the existing `profiles/*.json`), write
   `my-library.json`, and finish with the next command pre-filled: `curator plan -p my-library.json`.
   Zero-config default: `curator plan ~/Downloads` builds an in-memory default profile (read-only, so safe).
4. Always end a read-only command with the exact next step (the CLI already does this for `plan` — extend it to `scan`, `undo`, `recover`).

**Real-time feedback** (✅ implemented in `core.progress`): one live line — phase, `done/total (pct)`, bytes,
ETA from a smoothed rate, elapsed, middle-elided current path; plain periodic lines when stdout/stderr isn't a TTY;
`--progress json` for wrappers; final summary block (changed / unchanged / review / failed / bytes, duration, undo command).
Gap: scan has no total — a cheap first pass counting directory entries is not worth it; show `files · dirs · MB/s` instead.

**Errors** (✅ implemented in `core.errors`): every user-visible failure is `error: <summary> / why: <cause> / fix: <command>`,
with `details:` under `-v` and the log path printed on unexpected errors. Examples now produced:

```
error: The destination is full.
  why: There is not enough free space.
  fix: Free up space or choose another destination, then `curator recover --resume`.
```
```
error: The profile file is not valid JSON.
  why: line 7, column 3: Expecting property name enclosed in double quotes
  fix: Fix the syntax, or write a fresh starter with `curator init-profile my.json -s <folder>`.
```

Next: attach a `fix` at every `raise CuratorError(...)` site in `profiles.py`, `planner.py` and `executor.py`
(`PreflightError` messages already carry a reconnect hint — promote those to `fix`).

---

## Phase 4 — What was delivered in this branch (`audit/modernization`, uncommitted)

| File | Change |
|---|---|
| `src/datacurator/core/{errors,lifecycle,progress,logs}.py` | New runtime services (see above). |
| `src/datacurator/storage/walk.py` | New safe traversal: deterministic, cancellable, loop/mount/symlink/permission-aware, one stat per dir, no per-file `realpath`. Reports `WalkIssue`s and `complete`. |
| `src/datacurator/engine/batch.py` | New `run_resilient`: bounded in-flight window, retry with jittered exponential backoff, interruptible sleeps, fatal-error abort, per-item outcomes. |
| `src/datacurator/cli.py` | `main()` uses the coordinator + central error renderer + file logging; `-v/-vv`; scan/plan/run honour Ctrl-C/SIGTERM; cancelled scan no longer feeds `plan`; ad-hoc signal code removed from `_apply`; `override` validates input; progress via shared sink. |
| `engine/scanner.py` | `realpath` hoisted out of the per-file loop (P1). |
| `engine/journal.py`, `engine/executor.py` | `Journal.kept_partial` replaces the quadratic scan in `partial_for` (P2). |
| `storage/fsops.py`, `engine/util.py` | `fsync_dir` moved to storage; `engine.util` re-exports it (A1). |
| `tests/test_core_runtime.py`, `tests/conftest.py` | 23 tests for the new modules; autouse fixture points `DATACURATOR_LOG_DIR` at `tmp_path`. |

**Verification status:** all files compile on 3.12; a stdlib smoke script exercising errors, signals (SIGTERM
twice → exit 143), progress adapter, retry, disk-full abort, parallel batch and walk (symlink, loop) passed.
**The pytest suite (new and existing) has not been run** — run `python -m pip install -e ".[gui,dev]" && python -m pytest`
before merging. Known behavioural changes to review: error text for `CuratorError` now prints as `error: …`
(was bare), `plan`/`dedupe`/`run` return exit code 130 on a cancelled scan, `-v` is a new global flag.

## Roadmap (ordered by value ÷ risk)

1. Run the full suite on macOS + add Linux and Windows jobs to `.github/workflows`; add `ruff` + `mypy --strict core storage/walk.py engine/batch.py` + `import-linter`.
2. Unify cancellation: one `Cancelled` in `core.errors`, raised by scanner/planner/executor/`hash_file`; remove `InterruptedError` usage.
3. Migrate `scanner._scan_root` onto `storage.walk.walk` and stream metadata futures across directories (P3, P8).
4. Typed `Profile` (dataclasses + `schema` version + `migrate()`), keeping JSON compatibility (A5).
5. Move orchestration out of the CLI into `Engine.run_unattended()`; thin CLI/GUI (A6).
6. Copy path: larger buffer + hash thread; make post-rename re-hash opt-in (P4, P5) — needs your call on the safety trade-off.
7. Journal group commit for same-volume renames (P6), with a crash-injection test via the `LocalFS` seam.
8. Split `planner.py` by category; split `gui/app.py` by page.
9. First-run wizard, `curator doctor`, shell completion, optional dependency extras.
