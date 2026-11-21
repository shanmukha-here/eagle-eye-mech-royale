#!/usr/bin/env python3
"""Local placement board. Run next to an unpacked kit or pass --kit. Python 3.9+, cc, git."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import gzip
import hashlib
import inspect
import importlib.util
import json
import math
import multiprocessing
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import time

from local_sim.game import Match
from local_sim.geometry import Map
from local_sim.pool import roster, scripted
from local_sim.wire import Client

ROOT = Path(__file__).resolve().parent
REFS = {
    "phase1": "f4e346ad250f0ca69a4803e760e2ab482f2f4cd6",
    "phase2": "30cd61654c5d03c2a3131448259c9b4603796fd5",
}
SPLITS = {
    "cal": (170000, 96),
    "dev": (420000, 96),
    "holdout": (860000, 200),
    "devcheck": (520000, 256),
    "confirm": (960000, 400),
}
DEFAULT_MIX = 3


def load_module(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare(args):
    global HELLO, GEO, FACTORIES, CMD, HASHES, OUT, ENGINE
    kit = args.kit.resolve()
    with gzip.open(kit / "check/stream.jsonl.gz", "rt") as f:
        HELLO = json.loads(next(f))
    HELLO["map"]["dir"] = str(kit / "arena")
    cache = args.cache.resolve()
    cache.mkdir(parents=True, exist_ok=True)

    def digest(files):
        d = hashlib.sha256()
        for f in files:
            d.update(f.read_bytes())
        return d.hexdigest()[:20]

    inputs = list(sorted((ROOT / "local_sim").glob("*.py"))) + [
        ROOT / "local_sim/geometry.c",
        Path(__file__),
        kit / "arena/v2/world.json",
        kit / "arena/v2/terrain.i16",
        kit / "check/stream.jsonl.gz",
        kit / "python/example_bot.py",
        kit / "python/mechfight.py",
    ]
    # Reporting and calibration metadata cannot change an existing match result.
    inputs.remove(Path(__file__))
    h = hashlib.sha256(
        digest(inputs).encode() + inspect.getsource(run_match).encode() + str(REFS).encode()
    )
    ENGINE = h.hexdigest()[:20] + ("-docker" if args.docker else "-native")
    lib = cache / ("geometry-" + digest([ROOT / "local_sim/geometry.c"]) + ".so")
    if not lib.exists():
        tmp = lib.with_suffix("." + str(os.getpid()) + ".tmp")
        subprocess.run(
            ["cc", "-O3", "-shared", "-fPIC", str(ROOT / "local_sim/geometry.c"), "-lm", "-o", str(tmp)],
            check=True,
            env={**os.environ, "TMPDIR": str(cache)},
        )
        tmp.replace(lib)
    FACTORIES = {}
    CMD = {}
    HASHES = {}
    for name in [*REFS, "candidate"]:
        if name == "candidate":
            folder = args.candidate_dir.resolve()
        else:
            folder = cache / "sources" / REFS[name]
            folder.mkdir(parents=True, exist_ok=True)
            for file in ("arena.py", "bot.py", "Dockerfile", ".dockerignore"):
                data = subprocess.check_output(["git", "-C", str(ROOT), "show", REFS[name] + ":" + file])
                if not (folder / file).exists() or (folder / file).read_bytes() != data:
                    (folder / file).write_bytes(data)
        code = digest(
            [folder / "bot.py", folder / "arena.py", folder / "Dockerfile", folder / ".dockerignore"]
        )
        HASHES[name] = code
        if name == "candidate":
            snapshot = cache / "sources" / code
            snapshot.mkdir(parents=True, exist_ok=True)
            for file in ("bot.py", "arena.py", "Dockerfile", ".dockerignore"):
                if not (snapshot / file).exists():
                    shutil.copyfile(folder / file, snapshot / file)
            folder = snapshot
        if name == "phase1":
            frozen = load_module("localboard_fixed_arena", folder / "arena.py")
            GEO = Map(kit / "arena", HELLO["config"], lib, frozen.Arena)
        if name in REFS:
            m = load_module("opponent_" + name, folder / "bot.py")
            m.Arena = lambda *_: GEO.arena
            FACTORIES[name] = m.Bot
        if args.docker:
            tag = "eagle-eye-localboard:" + code
            subprocess.run(
                ["docker", "build", "--platform", "linux/amd64", "-q", "-t", tag, str(folder)], check=True
            )
            CMD[name] = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--network",
                "none",
                "--cpus",
                "1",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cap-drop",
                "ALL",
                "--pids-limit",
                "128",
                "--read-only",
                "--tmpfs",
                "/tmp:rw,size=64m",
                "--user",
                "65534:65534",
                "-v",
                str(kit / "arena") + ":/arena:ro",
                tag,
            ]
        else:
            CMD[name] = [sys.executable, "-B", str(folder / "bot.py")]
    sys.path.insert(0, str(kit / "python"))
    import mechfight

    saved = mechfight.run
    try:
        mechfight.run = lambda factory: None
        FACTORIES["starter"] = load_module("kit_example", kit / "python/example_bot.py").Bot
    finally:
        mechfight.run = saved
    OUT = cache / "matches" / ENGINE / ("ffa" if args.ffa else "squad") / str(args.mix)
    OUT.mkdir(parents=True, exist_ok=True)
    if not args.skip_gate:
        for key in dict.fromkeys(HASHES[n] for n in args.models):
            name = next(n for n in args.models if HASHES[n] == key)
            cmd = [
                sys.executable,
                str(kit / "check/check.py"),
                "--map-dir",
                "/arena" if args.docker else str(kit / "arena"),
                "--",
                *CMD[name],
            ]
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            if result.returncode:
                raise SystemExit("REJECTED before matches: " + name + "\n" + result.stdout)
            print("gate", name, "PASS", flush=True)


def pin_worker(cpus):
    # Each active match and its bot share one CPU. Parent preloads immutable map pages.
    n = multiprocessing.current_process()._identity[-1] - 1
    os.sched_setaffinity(0, {cpus[n % len(cpus)]})


def run_match(job):
    seed, name, mix, ffa, docker = job
    dest = OUT / (HASHES[name] + "-" + str(seed) + ".json")
    if dest.exists():
        return json.loads(dest.read_text())
    g = Match(GEO, HELLO, seed, roster(mix), ffa)
    subject = g.players[g.subject]

    def hello(i):
        return dict(HELLO, slot=i, seed=(i + 1) * 1779033703 % 2**32)

    for p in g.players:
        if p.role in FACTORIES:
            p.controller = FACTORIES[p.role](hello(p.id))
    h = hello(subject.id)
    if docker:
        h = dict(h, map=dict(h["map"], dir="/arena"))
    client = Client(CMD[name], h)
    try:
        while True:
            g.refresh_team()
            for p in g.players:
                if not p.life:
                    continue
                if p is subject or p.controller:
                    if p.role == "starter":
                        # The kit policy reads only these observation fields.
                        s = p.s
                        if ffa:
                            others = [
                                q
                                for q in g.players
                                if q is not p and q.life and g.visible(p, q.s["pos"], 9)
                            ]
                        else:
                            others = [subject] if p.id in g.active and g.t - g.team_seen < 5 else []
                        o = {
                            "tick": g.tick,
                            "self": s,
                            "zone": g.zone,
                            "offer": p.offer,
                            "mechs": [q.actor() for q in others],
                            "pickups": [
                                v
                                for v in g.pickups.values()
                                if abs(v["pos"][0] - s["pos"][0]) < 4
                                and abs(v["pos"][1] - s["pos"][1]) < 4
                            ],
                        }
                        import mechfight

                        mechfight.draw.clear()
                    else:
                        o = g.obs(p)
                    p.action = client.act(o) if p is subject else p.controller.act(o)
                else:
                    p.action = scripted(g, p)
                p.events = []
            place = g.advance()
            if place is None:
                place = g.advance()
            if place is not None:
                break
        client.end(place, subject.kills)
    finally:
        client.close()
    row = {
        "seed": seed,
        "points": 16 - place,
        "place": place,
        "time": round(g.t, 2),
        "kills": subject.kills,
        "cubes": subject.s["cubes"],
        "drops": subject.drops,
        "cause": subject.hit_by,
        "late": client.late,
        "bad": client.bad,
    }
    tmp = dest.with_suffix("." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(dest)
    return row


def interval(values):
    mean = statistics.mean(values)
    # Round the degrees of freedom down for a conservative two-sided
    # Student interval. The production split sizes have explicit critical values.
    n = len(values)
    critical = (
        (199, 1.972),
        (95, 1.986),
        (60, 2.001),
        (40, 2.022),
        (30, 2.043),
        (20, 2.086),
        (15, 2.132),
        (10, 2.229),
        (7, 2.365),
        (5, 2.571),
        (4, 2.777),
        (3, 3.183),
        (2, 4.303),
        (1, 12.707),
    )
    if n == 1:
        return [mean, None, None]
    crit = next(t for df, t in critical if n - 1 >= df)
    half = crit * statistics.stdev(values) / math.sqrt(n)
    return [mean, mean - half, mean + half]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--candidate-dir", type=Path, default=ROOT, help="evaluate this bot source directory")
    ap.add_argument(
        "--kit",
        type=Path,
        default=Path(os.environ.get("MECHFIGHT_KIT", ROOT.parent / "brief/kit/mechfight-kit")),
    )
    ap.add_argument("--cache", type=Path, default=ROOT.parent / "runs/localboard")
    ap.add_argument("--split", choices=[*SPLITS, "all"], default="all")
    ap.add_argument(
        "--test-split",
        choices=("holdout", "confirm"),
        default="holdout",
        help="test block for --split all",
    )
    ap.add_argument("--matches", type=int, help="smoke/pilot override per split")
    ap.add_argument("--models", nargs="+", choices=[*REFS, "candidate"], default=[*REFS, "candidate"])
    ap.add_argument("--mix", type=int, choices=range(5), default=DEFAULT_MIX)
    ap.add_argument("--workers", type=int, help="default: 6 in Docker, 48 native")
    ap.add_argument(
        "--ffa", action="store_true", help="final-style independent opponents; separate cache"
    )
    runtime = ap.add_mutually_exclusive_group()
    runtime.add_argument(
        "--docker", dest="docker", action="store_true", help="require the competition Docker sandbox"
    )
    runtime.add_argument(
        "--native",
        dest="docker",
        action="store_false",
        help="one CPU with a 512 MiB process address-space limit",
    )
    ap.set_defaults(docker=None)
    ap.add_argument("--skip-gate", action="store_true", help="diagnostic microbenchmark only")
    ap.add_argument("--report", type=Path)
    ap.add_argument("--ratio", type=float, default=None, help="use recorded median board/local ratio")
    ap.add_argument("--calibration", type=Path, default=ROOT.parent / "CALIBRATION.md")
    args = ap.parse_args()
    if args.docker is None:
        args.docker = shutil.which("docker") is not None
    if args.workers is None:
        args.workers = 6 if args.docker else 48
    if args.ratio is None and not args.ffa and args.calibration.is_file():
        ratios = []
        for line in args.calibration.read_text().splitlines():
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) >= 4 and cols[0].startswith("`"):
                try:
                    ratios.append(float(cols[2]) / float(cols[1]))
                except (ValueError, ZeroDivisionError):
                    pass
        if ratios:
            args.ratio = statistics.median(ratios)
    if args.matches is not None and args.matches < 1:
        ap.error("matches must be positive")
    if args.workers < 1:
        ap.error("workers must be positive")
    cpus = sorted(os.sched_getaffinity(0))
    start = time.monotonic()
    prepare(args)
    workers = min(args.workers, len(cpus))
    print("engine", ENGINE, "mix", args.mix, "workers", workers, "reference sources", REFS, flush=True)
    report = {}
    if args.ratio is None:
        reason = (
            "FFA has no practice calibration" if args.ffa else "No calibration ledger or --ratio supplied"
        )
        print(f"{reason}. Scores below are raw placement points.", flush=True)
    with ProcessPoolExecutor(
        workers, mp_context=multiprocessing.get_context("fork"), initializer=pin_worker, initargs=(cpus,)
    ) as pool:
        for split in ("dev", args.test_split) if args.split == "all" else [args.split]:
            first, default = SPLITS[split]
            n = args.matches or default
            jobs = {}
            for name in args.models:
                if HASHES[name] not in jobs:
                    jobs[HASHES[name]] = [
                        pool.submit(run_match, (first + i, name, args.mix, args.ffa, args.docker))
                        for i in range(n)
                    ]
            groups = {name: [f.result() for f in jobs[HASHES[name]]] for name in args.models}
            out = {}
            for name, rows in groups.items():
                values = [r["points"] for r in rows]
                score = interval(values)
                out[name] = {
                    "n": n,
                    "score_ci95": score,
                    "total_points": sum(values),
                    "late": sum(r["late"] for r in rows),
                    "mean_cubes": statistics.mean(r["cubes"] for r in rows),
                    "mean_kills": statistics.mean(r["kills"] for r in rows),
                }
                print(split, name, json.dumps(out[name]), flush=True)
            if "candidate" in groups and "phase2" in groups:
                delta = [a["points"] - b["points"] for a, b in zip(groups["candidate"], groups["phase2"])]
                out["paired_gain_ci95"] = interval(delta)
                if split == "confirm" and n == 400:
                    # Two-sided 97.5% Student critical value rounded upward for df=399.
                    mean = statistics.mean(delta)
                    half = 2.251 * statistics.stdev(delta) / math.sqrt(n)
                    out["paired_gain_ci97_5"] = [mean, mean - half, mean + half]
                    print(split, "paired 97.5% interval", out["paired_gain_ci97_5"], flush=True)
                print(split, "paired candidate minus phase2", out["paired_gain_ci95"], flush=True)
            if args.ratio is not None and "candidate" in out:
                out["calibrated_prediction"] = args.ratio * out["candidate"]["score_ci95"][0]
                out["calibrated_seed_ci95"] = [
                    v * args.ratio if v is not None else None for v in out["candidate"]["score_ci95"][1:]
                ]
                print(
                    split,
                    "calibrated placement prediction",
                    out["calibrated_prediction"],
                    "seed CI",
                    out["calibrated_seed_ci95"],
                    flush=True,
                )
            report[split] = out
    report.update(elapsed_s=round(time.monotonic() - start, 2), engine=ENGINE, mix=args.mix)
    report.update(
        runtime="docker" if args.docker else "native",
        ratio=args.ratio,
        workers=workers,
        sources={name: HASHES[name] for name in args.models},
    )
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print("elapsed seconds", report["elapsed_s"], flush=True)


if __name__ == "__main__":
    main()
