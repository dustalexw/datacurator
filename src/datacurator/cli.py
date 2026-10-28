"""Command-line interface. Shares the exact planning/execution engine with the GUI."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time

from . import __version__
from . import profiles as profiles_mod
from .engine.api import Engine
from .engine.executor import Control, fmt_bytes
from .engine.planstore import FILTERS, explain
from .models import CuratorError, PreflightError
from .naming.template import TOKENS_BY_CATEGORY, Template


def _engine(a) -> Engine:
    return Engine(a.data_dir)


def _load_profile(a) -> dict:
    if not a.profile:
        raise CuratorError("--profile is required (see `curator init-profile`).")
    p = profiles_mod.load(a.profile)
    for s in getattr(a, "source", None) or []:
        p["sources"].append(os.path.abspath(s))
    if not p["sources"]:
        raise CuratorError("The profile has no sources.")
    return p


def _progress_printer(quiet: bool):
    last = [0.0]

    def cb(*args):
        if quiet:
            return
        now = time.monotonic()
        if now - last[0] < 0.5:
            return
        last[0] = now
        if len(args) == 2:
            d, n = args
            print(f"\r  {n} files… {d[-60:]}".ljust(80), end="", file=sys.stderr, flush=True)
        else:
            ev = args[0]
            print(f"\r  {ev['done']}/{ev['total']}  {fmt_bytes(ev.get('bytes', 0))} copied".ljust(80), end="",
                  file=sys.stderr, flush=True)
    return cb


def _print_summary(store) -> None:
    s = store.summary()
    print(f"Plan {store.plan_id}  ({store.path})")
    print(f"  changes: {s['changes']}   unchanged: {s['unchanged']}   review: {s['review']}   skipped: {s['skipped']}")
    print(f"  conflicts: {s['conflicts']}   duplicates: {s['duplicates']}   errors: {s['errors']}   "
          f"name adjustments: {s['sanitized']}   preservation notes: {s['limitations']}")
    print(f"  new folders: {s['folders_to_create']}   data to copy across drives: {fmt_bytes(s['bytes_to_copy'])}")


def _print_items(store, filt: str, text: str, limit: int) -> None:
    for row in store.page(filt, text, 0, limit):
        it = store.get(row["id"])
        arrow = f"\n      → {it.dest}" if it.dest and it.dest != it.source else ""
        flags = f" [{', '.join(it.flags)}]" if it.flags else ""
        print(f"#{it.id:<5} {it.action.upper():<9}{flags} {it.source}{arrow}\n      {it.reason}")
    n = store.count(filt, text)
    if n > limit:
        print(f"… {n - limit} more (use --limit)")


def cmd_scan(a):
    e = _engine(a)
    p = _load_profile(a)
    t = time.time()
    st = e.scan(p, progress=_progress_printer(a.quiet))
    if not a.quiet:
        print("\r" + " " * 80 + "\r", end="", file=sys.stderr, flush=True)
    print(f"Scanned {st.files} files in {st.dirs} folders in {time.time() - t:.1f}s "
          f"(metadata read: {st.extracted}, reused from catalog: {st.reused}, removed: {st.removed}).".ljust(80))
    for x in st.errors[:20]:
        print("  ! " + x)
    if st.cycles:
        print(f"  symlink loops skipped: {len(st.cycles)}")
    if st.skipped_mounts:
        print(f"  other filesystems not entered (enable cross_mounts to include): {len(st.skipped_mounts)}")
    if st.excluded_folders:
        print(f"  protected folders left exactly as they are: {len(st.excluded_folders)}"
              " (add a path to scan.allow_dirs in the profile to organize it anyway)")
        for path, why in st.excluded_folders[:20]:
            print(f"    - {path}: {why}")
    return 0


def cmd_plan(a):
    e = _engine(a)
    p = _load_profile(a)
    if not a.no_scan:
        cmd_scan(a)
    store = e.plan(p, progress=_progress_printer(a.quiet))
    print("\r" + " " * 80 + "\r", end="", file=sys.stderr)
    _print_summary(store)
    if a.show:
        _print_items(store, a.show, a.search or "", a.limit)
    if a.csv:
        store.export_csv(a.csv)
        print(f"Report written to {a.csv}")
    if a.json:
        store.export_json(a.json)
        print(f"Report written to {a.json}")
    print(f"\nNothing has been changed. Apply with: curator apply {store.plan_id}")
    return 0


def cmd_show(a):
    store = _engine(a).open_plan(a.plan)
    _print_summary(store)
    _print_items(store, a.filter, a.search or "", a.limit)
    return 0


def cmd_explain(a):
    store = _engine(a).open_plan(a.plan)
    it = store.get(a.item)
    if not it:
        raise CuratorError(f"No item #{a.item}")
    print(f"{it.source}\n" + (f"→ {it.dest}\n" if it.dest else "") + explain(it))
    return 0


def _apply(e: Engine, store, a, unattended=False):
    if not unattended and not a.yes:
        _print_summary(store)
        ans = input("Apply these changes? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("Nothing changed.")
            return 1
    control = Control()

    def on_sigint(sig, frame):
        print("\nCancelling after the current file…", file=sys.stderr)
        control.cancel()
    old = signal.signal(signal.SIGINT, on_sigint)
    try:
        res = e.apply(store, progress=_progress_printer(a.quiet), control=control,
                      approve_deletes=getattr(a, "approve_deletes", False))
    finally:
        signal.signal(signal.SIGINT, old)
    print(f"\rBatch {res.batch_id}: {res.status}. {res.message}".ljust(80))
    for src, why in (res.preflight + res.errors)[:50]:
        print(f"  - {src}: {why}")
    if res.status in ("completed", "completed_with_errors", "cancelled"):
        print(f"Undo with: curator undo {res.batch_id}")
    if res.status == "interrupted":
        print("Resume with: curator recover --resume")
    return 0 if res.status == "completed" else 2


def cmd_apply(a):
    e = _engine(a)
    store = e.open_plan(a.plan)
    if e.journal.unfinished():
        print("An earlier batch was interrupted. Run `curator recover` first.")
        return 3
    try:
        return _apply(e, store, a)
    except PreflightError as ex:
        print("Cannot apply:\n  " + str(ex).replace("\n", "\n  "))
        return 4


def cmd_run(a):
    """Unattended: scan + plan + apply with an explicitly enabled profile."""
    e = _engine(a)
    p = _load_profile(a)
    profiles_mod.check_unattended(p)
    p["conflict_policy"] = p["unattended"]["conflict_policy"]
    if e.journal.unfinished():
        rep = e.recover(resume=bool(p["unattended"].get("resume_interrupted", True)))
        print(f"Recovered {len(rep.batches)} interrupted batch(es).")
    e.scan(p)
    store = e.plan(p)
    _print_summary(store)
    if store.count("changes") == 0:
        print("Nothing to do.")
        return 0
    try:
        return _apply(e, store, a, unattended=True)
    except PreflightError as ex:
        print("Cannot apply:\n  " + str(ex).replace("\n", "\n  "))
        return 4


def cmd_undo(a):
    e = _engine(a)
    rep = e.undo(a.batch, dry_run=a.dry_run)
    print(("Would restore" if a.dry_run else "Undo:") + " " + rep.summary())
    for n in rep.notes:
        print("  · " + n)
    return 0 if rep.complete else 2


def cmd_recover(a):
    e = _engine(a)
    rep = e.recover(resume=a.resume)
    if not rep.batches:
        print("No interrupted batches. Everything is consistent.")
        return 0
    for x in rep.actions:
        print("  ✓ " + x)
    for x in rep.attention:
        print("  ! " + x)
    print(f"Checked {len(rep.batches)} batch(es).")
    return 0


def cmd_history(a):
    e = _engine(a)
    for b in e.journal.batches(a.limit):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(b["created"]))
        print(f"{b['id']}  {when}  {b['kind']:<6} {b['status']:<22} {b.get('message') or ''}")
    return 0


def cmd_probe(a):
    from .storage.capabilities import probe
    e = _engine(a)
    c = probe(a.path)
    if c.probed == "active":
        e.catalog.store_capabilities(os.path.abspath(a.path), c.to_dict())
    d = c.to_dict()
    for k in ("fs_type", "probed", "case_sensitive", "normalization_insensitive", "windows_naming", "max_name_bytes",
              "max_name_units", "max_path", "max_file_size", "free_bytes", "read_only", "is_network",
              "supports_symlinks", "supports_hardlinks", "supports_xattr", "supports_ads"):
        print(f"  {k:<26} {d[k]}")
    for n in c.notes:
        print("  · " + n)
    return 0


def cmd_override(a):
    e = _engine(a)
    target = f"group:{a.group}" if a.group else os.path.abspath(a.path)
    for kv in a.values:
        if "=" not in kv:
            raise CuratorError(f"Expected field=value, got {kv!r}")
        k, v = kv.split("=", 1)
        e.catalog.set_override(target, k.strip(), v if v != "" else None)
    print(f"Overrides for {target}: {e.catalog.overrides().get(target, {})}")
    return 0


def cmd_accept(a):
    """Turn an item's suggested values into overrides."""
    e = _engine(a)
    store = e.open_plan(a.plan)
    n = 0
    for iid in a.items:
        it = store.get(iid)
        if not it or not it.suggestions:
            continue
        for k, v in it.suggestions.items():
            if not k.startswith("_") and " or " not in str(v):
                e.catalog.set_override(it.source, k, str(v))
                n += 1
    print(f"Accepted {n} suggested value(s). Re-run `curator plan` to see the result.")
    return 0


def cmd_exclude(a):
    e = _engine(a)
    for p in a.paths:
        e.catalog.set_excluded(os.path.abspath(p), not a.include)
    print(("Included" if a.include else "Excluded") + f" {len(a.paths)} path(s).")
    return 0


def cmd_tag(a):
    e = _engine(a)
    if a.action == "add":
        for p in a.paths:
            e.catalog.add_tag(os.path.abspath(p), a.tag)
    elif a.action == "remove":
        for p in a.paths:
            e.catalog.remove_tag(os.path.abspath(p), a.tag)
    for p in e.catalog.search(tag=a.tag, limit=a.limit):
        print(p)
    return 0


def cmd_collection(a):
    e = _engine(a)
    if a.action == "add":
        for p in a.paths:
            e.catalog.add_to_collection(a.name, os.path.abspath(p))
        print(f"Added {len(a.paths)} to “{a.name}”.")
    elif a.action == "show":
        for p in e.catalog.search(collection=a.name, limit=a.limit):
            print(p)
    else:
        for name, n in e.catalog.collections():
            print(f"{name}  ({n})")
    return 0


def cmd_search(a):
    for p in _engine(a).catalog.search(text=a.text, tag=a.tag, collection=a.collection, limit=a.limit):
        print(p)
    return 0


def cmd_locations(a):
    from .storage import locations as L
    from .storage.capabilities import volume_identity
    e = _engine(a)
    if a.action == "add":
        ident = volume_identity(a.path).to_dict() if os.path.isdir(a.path) else {}
        e.catalog.save_location(a.name, os.path.abspath(a.path), "share" if a.server else "local", a.protocol or "",
                                a.server or "", a.share or "", a.user or "", ident)
        if a.user and a.password_prompt:
            import getpass
            try:
                L.store_password(a.name, a.user, getpass.getpass(f"Password for {a.user}: "))
                print("Password stored in the operating system credential store.")
            except Exception as e:
                print(f"Could not store the password ({e.__class__.__name__}: {e}). "
                      "Install or unlock your system keyring, or connect with the OS dialog instead.")
        print(f"Saved location “{a.name}”.")
    elif a.action == "remove":
        e.catalog.remove_location(a.name)
    elif a.action == "connect":
        loc = next((l for l in e.catalog.locations() if l["name"] == a.name), None)
        if not loc:
            raise CuratorError(f"No saved location {a.name}")
        ok, msg = L.connect(loc)
        print(msg)
        return 0 if ok else 2
    else:
        for loc in e.catalog.locations():
            st = L.status(loc)
            mark = "●" if st.reachable and st.same_storage is not False else "○"
            print(f"{mark} {loc['name']:<16} {loc['path']}\n    {st.message}" + (f"\n    {st.guidance}" if st.guidance else ""))
    return 0


def cmd_dedupe(a):
    from .engine.dedupe import plan_dedupe
    e = _engine(a)
    p = _load_profile(a)
    if not a.no_scan:
        cmd_scan(a)
    store = plan_dedupe(e.catalog, p["sources"], a.quarantine, e.plans_dir,
                        prefer_roots=[c["root"] for c in p["categories"].values() if c.get("root")], profile=p)
    _print_summary(store)
    _print_items(store, "all", "", a.limit)
    print(f"\nApply with: curator apply {store.plan_id} --approve-deletes   (moves extra copies to {a.quarantine}; undoable)")
    return 0


def cmd_report(a):
    store = _engine(a).open_plan(a.plan)
    if a.csv:
        store.export_csv(a.csv)
    if a.json:
        store.export_json(a.json)
    print("Report written.")
    return 0


def cmd_presets(a):
    for cat, presets in profiles_mod.PRESETS.items():
        print(f"{cat}:")
        for name, pr in presets.items():
            print(f"  {name:<26} {pr['label']}")
        print(f"  tokens: {' '.join('{' + t + '}' for t in sorted(TOKENS_BY_CATEGORY[cat]))}")
    return 0


SAMPLES = {
    "music": {"album_artist": "Daft Punk", "artist": "Daft Punk feat. Pharrell Williams", "album": "Random Access Memories",
              "year": 2013, "disc": None, "track": 8, "title": "Get Lucky", "format": "FLAC", "genre": "Electronic"},
    "tv": {"series": "The Expanse", "series_year": 2015, "season": 1, "episode": "2", "episode_code": "S01E02",
           "episode_title": "The Big Empty", "resolution": "1080p", "source": "WEB-DL", "codec": "x265"},
    "movies": {"title": "Arrival", "year": 2016, "resolution": "2160p"},
    "documents": {"project": "Taxes 2025", "name": "receipt-0412"},
    "photos": {"year": 2024, "month": 7, "day": 4, "name": "IMG_0412", "camera_make": "Canon"},
    "other": {"name": "notes", "type": "TXT"},
}


def cmd_template(a):
    cat = a.category
    ext = {"music": ".flac", "tv": ".mkv", "movies": ".mkv", "documents": ".pdf", "photos": ".jpg"}.get(cat, ".txt")
    ft = Template(a.folder, "folder", cat)
    nt = Template(a.filename, "filename", cat)
    vals = dict(SAMPLES[cat], ext=ext, extension=ext)
    print("Example:          " + "/".join(filter(None, [ft.example(vals), nt.example(vals, ext)])))
    vals2 = {k: v for k, v in vals.items() if k not in ("year", "track", "episode_title", "project")}
    print("Missing values:   " + "/".join(filter(None, [ft.example(vals2), nt.example(vals2, ext)])))
    return 0


def cmd_init_profile(a):
    p = json.loads(json.dumps(profiles_mod.DEFAULT_PROFILE))
    p["name"] = a.name
    p["sources"] = [os.path.abspath(s) for s in a.source or []]
    for cat in ("music", "tv", "movies", "documents", "photos", "other"):
        root = getattr(a, f"{cat}_root")
        if root:
            p["categories"][cat]["root"] = os.path.abspath(root)
            p["categories"][cat]["enabled"] = True
    profiles_mod.normalize(p)
    profiles_mod.save(p, a.out)
    print(f"Profile written to {a.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="curator", description="Data Curator — deterministic file organizer.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data-dir", help="catalog/journal location (default: per-user app data)")
    ap.add_argument("-q", "--quiet", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def prof(sp):
        sp.add_argument("--profile", "-p")
        sp.add_argument("--source", "-s", action="append", help="extra source folder")

    sp = sub.add_parser("scan", help="scan sources into the catalog (read-only)")
    prof(sp)
    sp.set_defaults(fn=cmd_scan)
    sp = sub.add_parser("plan", help="scan and build a preview plan (read-only)")
    prof(sp)
    sp.add_argument("--no-scan", action="store_true")
    sp.add_argument("--show", choices=list(FILTERS), help="list items matching a filter")
    sp.add_argument("--search")
    sp.add_argument("--limit", type=int, default=100)
    sp.add_argument("--csv")
    sp.add_argument("--json")
    sp.set_defaults(fn=cmd_plan)
    sp = sub.add_parser("show", help="list items of a plan")
    sp.add_argument("plan")
    sp.add_argument("--filter", default="changes", choices=list(FILTERS))
    sp.add_argument("--search")
    sp.add_argument("--limit", type=int, default=100)
    sp.set_defaults(fn=cmd_show)
    sp = sub.add_parser("explain", help="plain-language explanation of one item")
    sp.add_argument("plan")
    sp.add_argument("item", type=int)
    sp.set_defaults(fn=cmd_explain)
    sp = sub.add_parser("apply", help="apply a plan")
    sp.add_argument("plan")
    sp.add_argument("--yes", "-y", action="store_true")
    sp.add_argument("--approve-deletes", action="store_true", help="required for duplicate-removal plans")
    sp.set_defaults(fn=cmd_apply)
    sp = sub.add_parser("run", help="unattended scan+plan+apply (profile must enable it)")
    prof(sp)
    sp.set_defaults(fn=cmd_run, yes=True)
    sp = sub.add_parser("undo", help="undo an applied batch")
    sp.add_argument("batch")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_undo)
    sp = sub.add_parser("recover", help="repair interrupted batches")
    sp.add_argument("--resume", action="store_true")
    sp.set_defaults(fn=cmd_recover)
    sp = sub.add_parser("history", help="list batches")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(fn=cmd_history)
    sp = sub.add_parser("probe", help="measure a destination's filesystem behaviour")
    sp.add_argument("path")
    sp.set_defaults(fn=cmd_probe)
    sp = sub.add_parser("override", help="set catalog-level metadata overrides (no file changes)")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--group")
    sp.add_argument("values", nargs="+", metavar="field=value")
    sp.set_defaults(fn=cmd_override)
    sp = sub.add_parser("accept", help="accept suggested values of review items")
    sp.add_argument("plan")
    sp.add_argument("items", type=int, nargs="+")
    sp.set_defaults(fn=cmd_accept)
    sp = sub.add_parser("exclude", help="exclude paths from organizing")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--include", action="store_true", help="remove the exclusion")
    sp.set_defaults(fn=cmd_exclude)
    sp = sub.add_parser("tag", help="catalog tags (no folders created)")
    sp.add_argument("action", choices=["add", "remove", "list"])
    sp.add_argument("tag")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(fn=cmd_tag)
    sp = sub.add_parser("collection", help="catalog collections (no folders created)")
    sp.add_argument("action", choices=["add", "show", "list"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(fn=cmd_collection)
    sp = sub.add_parser("search", help="search the catalog")
    sp.add_argument("text", nargs="?", default="")
    sp.add_argument("--tag")
    sp.add_argument("--collection")
    sp.add_argument("--limit", type=int, default=200)
    sp.set_defaults(fn=cmd_search)
    sp = sub.add_parser("locations", help="saved locations and share status")
    sp.add_argument("action", choices=["list", "add", "remove", "connect"], nargs="?", default="list")
    sp.add_argument("name", nargs="?")
    sp.add_argument("path", nargs="?")
    sp.add_argument("--protocol", choices=["smb", "nfs"])
    sp.add_argument("--server")
    sp.add_argument("--share")
    sp.add_argument("--user")
    sp.add_argument("--password-prompt", action="store_true")
    sp.set_defaults(fn=cmd_locations)
    sp = sub.add_parser("dedupe", help="plan quarantining exact duplicates (separate approval)")
    prof(sp)
    sp.add_argument("--quarantine", required=True)
    sp.add_argument("--no-scan", action="store_true")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(fn=cmd_dedupe)
    sp = sub.add_parser("report", help="export a plan as CSV/JSON")
    sp.add_argument("plan")
    sp.add_argument("--csv")
    sp.add_argument("--json")
    sp.set_defaults(fn=cmd_report)
    sp = sub.add_parser("presets", help="list template presets and tokens")
    sp.set_defaults(fn=cmd_presets)
    sp = sub.add_parser("template", help="try a template with example values")
    sp.add_argument("category", choices=list(TOKENS_BY_CATEGORY))
    sp.add_argument("--folder", required=True)
    sp.add_argument("--filename", required=True)
    sp.set_defaults(fn=cmd_template)
    sp = sub.add_parser("init-profile", help="write a starter profile")
    sp.add_argument("out")
    sp.add_argument("--name", default="My library")
    sp.add_argument("--source", "-s", action="append")
    for cat in ("music", "tv", "movies", "documents", "photos", "other"):
        sp.add_argument(f"--{cat}-root")
    sp.set_defaults(fn=cmd_init_profile)
    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv)
    try:
        return a.fn(a) or 0
    except CuratorError as e:
        print(str(e), file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
