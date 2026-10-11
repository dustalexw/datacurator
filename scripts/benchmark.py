"""Large-library benchmark: generates a synthetic library, then measures scan,
incremental rescan, planning, apply and undo. Prints JSON with hardware info.

usage: python scripts/benchmark.py [--albums 2500] [--episodes 10000] [--docs 5000] [--dir /tmp/bench]
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import shutil
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tests"))

from mutagen.flac import FLAC  # noqa: E402

from datacurator.engine.api import Engine  # noqa: E402
from fixtures import template  # noqa: E402


def hardware() -> dict:
    import psutil
    cpu = platform.processor() or ""
    try:
        for line in open("/proc/cpuinfo"):
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    return {"cpu": cpu, "logical_cpus": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
            "os": f"{platform.system()} {platform.release()}", "python": platform.python_version()}


def generate(root: str, albums: int, episodes: int, docs: int, episode_kb: int = 0) -> int:
    tpl = template(".flac")
    n = 0
    for a in range(albums):
        d = os.path.join(root, "incoming", f"rip{a:05d}")
        os.makedirs(d, exist_ok=True)
        for t in range(1, 13):
            p = os.path.join(d, f"track{t:02d}.flac")
            shutil.copyfile(tpl, p)
            f = FLAC(p)
            f["TITLE"] = f"Song {t} of album {a}"
            f["ARTIST"] = f"Artist {a % 900}"
            f["ALBUMARTIST"] = f"Artist {a % 900}"
            f["ALBUM"] = f"Album {a}"
            f["TRACKNUMBER"] = str(t)
            f["DATE"] = str(1960 + a % 60)
            f.save()
            n += 1
        with open(os.path.join(d, "cover.jpg"), "wb") as fh:
            fh.write(b"\xff\xd8" + a.to_bytes(4, "big"))
        n += 1
    tv = os.path.join(root, "incoming", "tv")
    os.makedirs(tv, exist_ok=True)
    for e in range(episodes):
        show, season, ep = e // 200, (e // 20) % 10 + 1, e % 20 + 1
        base = os.path.join(tv, f"Show.{show:03d}.S{season:02d}E{ep:02d}.Episode.{e}.1080p.WEB-DL")
        with open(base + ".mkv", "wb") as fh:
            fh.write(e.to_bytes(4, "big") * 64 + (os.urandom(episode_kb * 1024) if episode_kb else b""))
        n += 1
        if e % 2 == 0:
            with open(base + ".en.srt", "wb") as fh:
                fh.write(b"1\n")
            n += 1
    for i in range(docs):
        d = os.path.join(root, "incoming", "docs", f"Project {i % 50}")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"doc{i:05d}.pdf"), "wb") as fh:
            fh.write(b"%PDF" + i.to_bytes(4, "big"))
        n += 1
    return n


def timed(fn):
    t = time.perf_counter()
    r = fn()
    return r, round(time.perf_counter() - t, 2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--albums", type=int, default=2500)
    ap.add_argument("--episodes", type=int, default=10000)
    ap.add_argument("--docs", type=int, default=5000)
    ap.add_argument("--dir", default="/tmp/curator-bench")
    ap.add_argument("--dest", help="put the organized library on another filesystem (cross-volume copies)")
    ap.add_argument("--episode-kb", type=int, default=0, help="size of each synthetic episode file")
    a = ap.parse_args()
    shutil.rmtree(a.dir, ignore_errors=True)
    os.makedirs(a.dir)
    (n, t_gen) = timed(lambda: generate(a.dir, a.albums, a.episodes, a.docs, a.episode_kb))
    eng = Engine(os.path.join(a.dir, "appdata"))
    lib = os.path.join(a.dir, "incoming")
    out = os.path.join(a.dest or a.dir, "Library")
    if a.dest:
        shutil.rmtree(out, ignore_errors=True)
    prof = {"sources": [lib], "categories": {
        "music": {"root": os.path.join(out, "Music")}, "tv": {"root": os.path.join(out, "TV")},
        "documents": {"enabled": True, "root": os.path.join(out, "Documents")}}}
    st, t_scan = timed(lambda: eng.scan(prof))
    st2, t_rescan = timed(lambda: eng.scan(prof))
    store, t_plan = timed(lambda: eng.plan(prof))
    s = store.summary()
    res, t_apply = timed(lambda: eng.apply(store))
    prof2 = dict(prof, sources=[os.path.join(out, x) for x in ("Music", "TV", "Documents")])
    _, t_scan_out = timed(lambda: eng.scan(prof2))
    again, t_replan = timed(lambda: eng.plan(prof2))
    rep, t_undo = timed(lambda: eng.undo(res.batch_id))
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    report = {
        "hardware": hardware(),
        "storage": ("cross-volume: ext4 virtual disk -> " + a.dest + " (verified copies)") if a.dest else
                   "single local ext4 virtual disk (same-volume renames; no cross-volume copies)",
        "bytes_copied": res.bytes_copied,
        "copy_mb_per_s": round(res.bytes_copied / 1024**2 / t_apply, 1) if a.dest and t_apply else None,
        "files": n,
        "generate_s": t_gen,
        "cold_scan_s": t_scan, "cold_scan_files_per_s": round(st.files / t_scan),
        "metadata_read": st.extracted,
        "incremental_rescan_s": t_rescan, "incremental_reused": st2.reused,
        "plan_s": t_plan, "plan_changes": s["changes"], "plan_review": s["review"],
        "apply_s": t_apply, "apply_done": res.done, "apply_failed": res.failed,
        "apply_files_per_s": round(res.done / t_apply) if t_apply else None,
        "replan_in_place_s": t_replan, "replan_changes": again.count("changes"),
        "undo_s": t_undo, "undo_restored": rep.restored, "undo_complete": rep.complete,
        "peak_rss_mb": round(peak_mb),
    }
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
