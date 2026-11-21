#!/usr/bin/env python3
"""Local placement board. Run next to an unpacked kit or pass --kit. Python 3.9+, cc, git."""

import argparse
from concurrent.futures import ProcessPoolExecutor
import fcntl
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
import tempfile
import time

from local_sim.game import Match
from local_sim.geometry import Map
from local_sim.pool import QUALIFIERS, qualifying_controller, roster, scripted
from local_sim.wire import Client

ROOT = Path(__file__).resolve().parent
REFS = {
    "lifeline": "8dec71a03afdc7d2e73335fe1fee1fee89d57303",
    "stride": "4e3c51f7a4f5718aed2e862deaae1c083e21ff6f",
    "mill": "bf3e794b82660464340df592701a01ee6ddfdfd2",
    "carry": "8fe2e27a2740b0cea9c96b9df9899d0c6105841c",

    "overlook": "8c370630a093ada974bae3ec669acf56ddef198d",
    "distance": "0cc24d0f151ba807422461e4f2e873d80a0f4139",
    "refine": "da47dd5d66dbfd337d012f011139c01d15d49ea1",
    "ambush": "1da5c09499f3ca9b5d452371c46f14f469f7be3f",
    "sweep": "fca4d43a303c5c0b3436aed5191ddc6fd78da4fe",
    "crystal": "b260cc20ef2ba57e8aa8d14569b944e1774a47bf",
    "rangegear": "7b2492ccb46b57f62e0e508f41e73e932259dfbb",
    "phase5": "6c7bb86707e76a288050da27e945f076f78227cc",
    "phase1": "f4e346ad250f0ca69a4803e760e2ab482f2f4cd6",
    "phase2": "30cd61654c5d03c2a3131448259c9b4603796fd5",
    "phase4": "55f14e7eeec28838eed4537948317dc8c385d893",
    "phase3": "c9901fa16f407aa222df52c184f858466bda13a4",
}
OPPONENT_REVISIONS = {
    "circle": "376dc9cd0be14ead83053b78ad6edf8a77047fd9",
    "equipment": "8165f67717e9c8c706933ee7e3110875d6b10268",
}
SPLITS = {
    "cal": (170000, 96),
    "dev": (420000, 96),
    "holdout": (860000, 200),
    "devcheck": (520000, 256),
    "encounter-dev": (620000, 256),
    "confirm": (960000, 400),
    "p4dev": (1200000, 96),
    "p4verify": (1300000, 200),
    "p4test": (1400000, 320),
    "p5dev": (1500000, 96),
    "p5verify": (1600000, 200),
    "p5test": (1700000, 320),
    "p5final": (1800000, 320),
    "p6dev": (1900000, 96),
    "p6verify": (2000000, 200),
    "p6test": (2100000, 640),
    "p7screen": (2200000, 48),
    "p7cma": (2300000, 24),
    "p7select": (2400000, 160),
    "p7test": (2500000, 400),
    "p8select": (2600000, 200),
    "p8test": (2700000, 200),
    "p9dev": (2800000, 64),
    "p9select": (2801000, 128),
    "p9test": (2900000, 400),
    "p10dev": (3000000, 64),
    "p10test": (3100000, 240),
    "p10rules": (3200000, 400),
    "p10combine": (3300000, 400),
    "p12dev": (3800000, 64),
    "p12test": (3900000, 240),
    "p12confirm": (4000000, 240),
    "p13dev": (5100000, 80),
    "p13test": (5300000, 240),
    "p13confirm": (5500000, 240),
    "p13aid": (5700000, 240),
    "chain-dev": (6050000, 80),
    "chain-test": (6200000, 240),
    "chain-confirm": (6400000, 240),
    "p12last": (4200000, 240),
    "p11diag": (3400000, 400),
    "p11dev": (3500000, 80),
    "p11test": (3600000, 240),
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
    # Keep per-match diagnostics on the chosen evaluation disk.
    tempfile.tempdir = str(cache)

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
        digest(inputs).encode()
        + inspect.getsource(run_match).encode()
        + str((REFS, OPPONENT_REVISIONS)).encode()
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
    revisions = {**REFS, **OPPONENT_REVISIONS}
    for name in [*revisions, "candidate"]:
        if name == "candidate":
            folder = args.candidate_dir.resolve()
        else:
            folder = cache / "sources" / revisions[name]
            folder.mkdir(parents=True, exist_ok=True)
            for file in ("arena.py", "bot.py", "Dockerfile", ".dockerignore"):
                data = subprocess.check_output(
                    ["git", "-C", str(ROOT), "show", revisions[name] + ":" + file]
                )
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
        if name in revisions:
            m = load_module("opponent_" + name, folder / "bot.py")
            m.Arena = lambda *_: GEO.arena
            FACTORIES[name] = m.Bot
        if name not in args.models:
            continue
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
    for role in ("laser_scout", "cube_farmer", "ring_camper"):
        FACTORIES[role] = qualifying_controller(role, FACTORIES["phase5"])
    sys.path.insert(0, str(kit / "python"))
    import mechfight

    saved = mechfight.run
    try:
        mechfight.run = lambda factory: None
        FACTORIES["starter"] = load_module("kit_example", kit / "python/example_bot.py").Bot
    finally:
        mechfight.run = saved
    OUT = cache / "matches" / ENGINE
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
    seed, name, mix, pool_name, docker = job
    ffa = pool_name != "practice"
    dest = OUT / pool_name / str(mix) / (HASHES[name] + "-" + str(seed) + ".json")
    if dest.exists():
        return json.loads(dest.read_text())
    roles = QUALIFIERS if pool_name == "qualifiers" else roster(mix, strong=pool_name == "strong")
    g = Match(GEO, HELLO, seed, roles, ffa)
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
    decisions = quiet = seen = 0
    try:
        while True:
            decisions += 1
            state = subject.s
            speed = math.hypot(*state["vel"][:2])
            quiet += state["grounded"] and 0.05 < speed < HELLO["config"]["sound"]["step_speed"]
            seen += any(p.life and p is not subject and g.visible(p, state["pos"], state["stats"]["height"])
                        for p in g.players)
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
        "decisions": decisions,
        "quiet_seconds": round(quiet * .1, 1),
        "seen_seconds": round(seen * .1, 1),
        "switch_frames": client.diagnostics,
        "seed": seed,
        "points": 16 - place,
        "place": place,
        "time": round(g.t, 2),
        "kills": subject.kills,
        "cubes": subject.s["cubes"],
        "drops": subject.drops,
        "cause": subject.fatal_weapon or ("win" if place == 1 else "clock"),
        "killer": subject.fatal_source,
        "killer_role": (
            g.players[subject.fatal_source].role
            if subject.fatal_source is not None and 0 <= subject.fatal_source < 16
            else None
        ),
        "zone_phase": g.phase + 1,
        "zone_radius": round(g.zone["radius"], 1),
        "outside": not subject.s["in_zone"],
        "ammo": subject.s["weapons"][0].get("ammo") if subject.s["weapons"][0] else None,
        "sidearm": subject.s["weapons"][1]["kind"] if subject.s["weapons"][1] else None,
        "late": client.late,
        "bad": client.bad,
    }
    tmp = dest.with_suffix("." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(row))
    tmp.replace(dest)
    return row


def shared_match(job):
    """Share one timed outcome when two evaluations request the same cold seed."""
    seed, name, mix, field, _ = job
    folder = OUT / field / str(mix)
    key = HASHES[name]
    result = folder / f"{key}-{seed}.json"
    if result.exists():
        return json.loads(result.read_text())
    # Fixed buckets bound the number of lock files in long-running studies.
    bucket = (seed + int(key[:6], 16)) % 4096
    with (folder / f".match-{bucket}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return run_match(job)  # Rechecks the cache after acquiring the lock.


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
        choices=("holdout", "confirm", "p4test", "p5test", "p5final", "p6test", "p7test", "p8test", "p9test", "p10test", "p10combine", "p11test", "p12test", "p12confirm", "p12last", "p13test", "p13confirm", "p13aid", "chain-test", "chain-confirm"),
        default="chain-test",
        help="test block for --split all",
    )
    ap.add_argument("--matches", type=int, help="smoke/pilot override per split")
    ap.add_argument("--models", nargs="+", choices=[*REFS, "candidate"], default=["lifeline", "sweep", "candidate"])
    ap.add_argument("--mix", type=int, choices=range(5), default=DEFAULT_MIX)
    ap.add_argument("--workers", type=int, help="default: 6 in Docker, 48 native")
    ap.add_argument(
        "--ffa", action="store_true", help="final-style independent opponents; separate cache"
    )
    ap.add_argument(
        "--pool", choices=("practice", "strong", "qualifiers", "both", "all"), help="phase 4 defaults to both frozen fields"
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
    if args.ffa and args.pool:
        ap.error("--ffa selects the legacy independent field; use --pool strong for competitor copies")
    pools = (
        ["ffa"] if args.ffa else (["practice", "strong", "qualifiers"] if args.pool in ("all", None) else (["practice", "strong"] if args.pool == "both" else [args.pool]))
    )
    if args.docker is None:
        args.docker = shutil.which("docker") is not None
    if args.workers is None:
        args.workers = 6 if args.docker else 48
    official_scores = {}
    if not args.ffa and args.calibration.is_file():
        ratios = []
        for line in args.calibration.read_text().splitlines():
            cols = [c.strip() for c in line.strip("|").split("|")]
            if len(cols) >= 4 and cols[0].startswith("`"):
                try:
                    official_scores[cols[0].strip("`")] = float(cols[2])
                    ratios.append(float(cols[2]) / float(cols[1]))
                except (ValueError, ZeroDivisionError):
                    pass
        if ratios and args.ratio is None:
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
    for pool_name in pools:
        (OUT / pool_name / str(args.mix)).mkdir(parents=True, exist_ok=True)
        print("opponent pool", pool_name, flush=True)
        pool_report = {}
        with ProcessPoolExecutor(
            workers,
            mp_context=multiprocessing.get_context("fork"),
            initializer=pin_worker,
            initargs=(cpus,),
        ) as pool:
            for split in (
                ({"chain-confirm": "chain-dev", "chain-test": "chain-dev", "p13aid": "p13dev", "p13confirm": "p13dev", "p13test": "p13dev", "p12last": "p12dev", "p12confirm": "p12dev", "p12test": "p12dev", "p11test": "p11dev", "p10test": "p10dev", "p10combine": "p10rules", "p9test": "p9dev", "p8test": "p8select", "p7test": "p7screen", "p6test": "p6dev", "p5final": "p5dev", "p5test": "p5dev", "p4test": "p4dev"}.get(args.test_split, "dev"), args.test_split)
                if args.split == "all"
                else [args.split]
            ):
                first, default = SPLITS[split]
                n = args.matches or default
                jobs = {}
                for name in args.models:
                    if HASHES[name] not in jobs:
                        jobs[HASHES[name]] = [
                            pool.submit(shared_match, (first + i, name, args.mix, pool_name, args.docker))
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
                        "placement_counts": {place: values.count(16 - place) for place in range(1, 17)},
                        "late": sum(r["late"] for r in rows),
                        "mean_cubes": statistics.mean(r["cubes"] for r in rows),
                        "mean_kills": statistics.mean(r["kills"] for r in rows),
                    }
                    print(split, name, json.dumps(out[name]), flush=True)
                references = [name for name in ("sweep", "lifeline", "stride", "mill", "carry", "overlook", "distance", "refine", "ambush", "phase5", "crystal", "rangegear", "phase4", "phase3", "phase2") if name in groups]
                reference = max(references, key=lambda name: official_scores.get(REFS[name][:7], -1), default=None)
                if "candidate" in groups and reference in groups:
                    delta = [
                        a["points"] - b["points"] for a, b in zip(groups["candidate"], groups[reference])
                    ]
                    out["paired_reference"] = reference
                    out["paired_finishes"] = {
                        "better": sum(d > 0 for d in delta),
                        "same": delta.count(0),
                        "worse": sum(d < 0 for d in delta),
                    }
                    out["paired_gain_ci95"] = interval(delta)
                    critical = {("confirm", 400): 2.251, ("p5final", 800): 2.246}.get((split, n))
                    if critical is not None:
                        # Round each predeclared two-sided 97.5% Student critical value upward.
                        mean = statistics.mean(delta)
                        half = critical * statistics.stdev(delta) / math.sqrt(n)
                        out["paired_gain_ci97_5"] = [mean, mean - half, mean + half]
                        print(split, "paired 97.5% interval", out["paired_gain_ci97_5"], flush=True)
                    print(split, "paired candidate minus", reference, out["paired_gain_ci95"], flush=True)
                if pool_name == "practice" and args.ratio is not None and "candidate" in out:
                    out["calibrated_prediction"] = args.ratio * out["candidate"]["score_ci95"][0]
                    out["calibrated_seed_ci95"] = [
                        v * args.ratio if v is not None else None
                        for v in out["candidate"]["score_ci95"][1:]
                    ]
                    print(
                        split,
                        "calibrated placement prediction",
                        out["calibrated_prediction"],
                        "seed CI",
                        out["calibrated_seed_ci95"],
                        flush=True,
                    )
                    if reference and "paired_gain_ci95" in out:
                        anchor = official_scores.get(REFS[reference][:7])
                        if anchor is not None:
                            out["anchor_score"] = anchor
                            out["anchored_prediction"], *out["anchored_seed_ci95"] = [
                                anchor + args.ratio * value if value is not None else None
                                for value in out["paired_gain_ci95"]
                            ]
                            print(split, "anchored practice forecast", out["anchored_prediction"],
                                  "paired seed CI", out["anchored_seed_ci95"], flush=True)
                pool_report[split] = out
        report[pool_name] = pool_report
        if len(pools) == 1:
            report.update(pool_report)  # Keep single-pool reports readable by older scripts.
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
