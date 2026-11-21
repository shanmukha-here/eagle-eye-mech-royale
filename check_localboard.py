"""Focused rule and failure regressions for the local board."""

import argparse
import math
import statistics
import gzip
import json
import os
from pathlib import Path
import sys
import tempfile
import subprocess

root = Path(__file__).resolve().parent
from local_sim.game import Match, new_weapon
from local_sim.geometry import Map
from local_sim.pool import roster
from local_sim.wire import Client

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "--kit",
    type=Path,
    default=Path(os.environ.get("MECHFIGHT_KIT", root.parent / "brief/kit/mechfight-kit")),
)
parser.add_argument(
    "--replay", type=Path, help="optional mechreplay/1 exhibition to validate map and clock"
)
args = parser.parse_args()
kit = args.kit.resolve()
with gzip.open(kit / "check/stream.jsonl.gz", "rt") as f:
    hello = json.loads(next(f))
hello["map"]["dir"] = str(kit / "arena")


def audit_replay(path, game):
    """Use announced centers to check the engine clock against every saved frame."""
    with gzip.open(path, "rt") as stream:
        h = json.loads(next(stream))
        assert h["format"] == "mechreplay/1" and h["tick_hz"] == 20 and h["frame_every"] == 2
        for objects, expected in (
            (h["mechs"], game.players),
            (h["npcs"], game.npcs),
            (h["crates"], game.crates),
            (h["pickups"], game.pickups),
            (h["drops"], game.drop_plan),
        ):
            assert len(objects) == len(expected)
        for drop, (call, _, _) in zip(h["drops"], game.drop_plan):
            assert drop["call_t"] == call and drop["land_t"] == call + game.cfg["drops"]["fall_s"]
        height_errors = []
        for actor in h["mechs"]:
            x, y, z = actor["spawn"]
            height_errors.append(abs(z - game.geo.floor_at(x, y)))
        assert len(h["zone"]) == len(game.zone_phases) + 1
        for i, (prev, nxt) in enumerate(zip(h["zone"], h["zone"][1:])):
            start, end, _, r, _, nr, dps = game.zone_phases[i]
            assert (start, end, r, nr) == (
                nxt["shrink_start"],
                nxt["shrink_end"],
                prev["radius"],
                nxt["radius"],
            )
            game.zone_phases[i] = (start, end, prev["center"], r, nxt["center"], nr, dps)
        count = 0
        largest = 0
        for line in stream:
            f = json.loads(line)
            if "z" not in f:
                continue
            game.t = f["t"] / h["tick_hz"]
            game.update_zone()
            expected = [*game.zone["center"], game.zone["radius"]]
            largest = max(largest, max(abs(a - b) for a, b in zip(f["z"], expected)))
            count += 1
        # Replay positions have two decimals.
        assert count and largest <= 0.02, (count, largest)
        print(
            f"PASS {count} public replay zone frames, maximum error {largest:.4f} m; spawn floor median/max error {statistics.median(height_errors):.3f}/{max(height_errors):.3f} m"
        )


def run():
    with tempfile.TemporaryDirectory() as td:
        lib = Path(td) / "geo.so"
        subprocess.run(
            ["cc", "-O3", "-shared", "-fPIC", str(root / "local_sim/geometry.c"), "-lm", "-o", str(lib)],
            check=True,
            env={**os.environ, "TMPDIR": td},
        )
        geo = Map(kit / "arena", hello["config"], lib)

        def fresh():
            return Match(geo, hello, 123456, roster(3))

        if args.replay:
            audit_replay(args.replay, fresh())
        from localboard import load_module

        schema = load_module("kit_schema", kit / "check/check.py").Schema(kit / "mechfight.schema.json")
        g = fresh()
        for _ in range(24):
            g.refresh_team()
            for q in g.players:
                obs = g.obs(q)
                errors = schema.errors(obs, "Observation")
                assert not errors, errors
                q.events = []
                q.action = {"fire": [True, False], "move": [0.5, -0.5]}
            g.advance()
            g.advance()
        print("PASS 384 generated observations against the official schema")
        g = fresh()
        p = g.players[g.subject]
        s = p.s
        assert len(g.players) == 16 and len(g.npcs) == 12 and len(g.crates) == 36 and len(g.pickups) == 40
        assert (
            min(
                math.dist(q.s["pos"][:2], r.s["pos"][:2])
                for q in g.players
                for r in g.players
                if q.id < r.id
            )
            >= 130
        )
        g.t = 415
        g.update_zone()
        assert g.zone["radius"] == 0 and g.zone["dps"] == 260
        g = fresh()
        p = g.players[g.subject]
        s = p.s
        g.gain(p, 3)
        g.offer(p)
        assert s["cubes"] == 3 and s["max_hp"] == 1568.0000000000002 and p.offer
        p.action = {"pick": 0}
        g.offer(p)
        assert len(s["upgrades"]) == 1
        pos = s["pos"]
        g.drops = {
            20000: {
                "id": 20000,
                "pos": pos[:],
                "radius": 8,
                "capture_s": 4,
                "lands_in": 0,
                "progress": 0,
                "holder": None,
            }
        }
        s["hp"] = 300
        s["energy"] = 15
        for _ in range(40):
            g.items([p])
            g.t += 0.05
        progress = g.drops[20000]["progress"]
        enemy = next(q for q in g.players if q is not p)
        enemy.s["pos"] = pos[:]
        for _ in range(20):
            g.items([p, enemy])
            g.t += 0.05
        assert g.drops[20000]["progress"] == progress
        for _ in range(42):
            g.items([p])
            g.t += 0.05
        assert (
            20000 not in g.drops
            and p.drops == 1
            and s["hp"] == s["max_hp"]
            and s["energy"] == s["max_energy"]
        )
        g = fresh()
        p = g.players[g.subject]
        p.s["weapons"][0]["ammo"] = 2
        p.action = {"drop": 0}
        g.items([p])
        item_id = p.events[-1]["id"]
        assert not schema.errors(p.events[-1], "DroppedEvent")
        p.action = {"take": item_id}
        g.items([p])
        assert p.s["weapons"][0]["ammo"] == 2
        for kind in ("gun", "mortar", "lightning", "laser"):
            g = fresh()
            p = g.players[g.subject]
            w = new_weapon(kind, g.cfg)
            p.s["weapons"] = [w, None]
            p.action = {"fire": [True, False]}
            for _ in range(15 if kind == "laser" else 1):
                g.fire(p)
            if kind == "laser":
                assert w["charge"] > 0
                p.action = {"fire": [False, False]}
                g.fire(p)
                assert w["charge"] == 0 and g.beams
            if kind in ("gun", "mortar"):
                assert len(g.bullets) == 1 and g.bullets[0][1] == kind
            if kind == "gun":
                w["ammo"] = 0
                p.action = {}
                g.fire(p)
                assert w["reload"] > 0
                for _ in range(50):
                    g.fire(p)
                assert w["ammo"] == 12
            if kind == "lightning":
                assert p.s["energy"] < 100
        g = fresh()
        p = g.players[g.subject]
        p.s["hp"] = -100
        g.players[g.order[0]].s["hp"] = -100
        assert g.advance() == 15, "simultaneous eliminations must share place"
        g = fresh()
        g.tick = 9599
        g.t = g.tick * 0.05
        for q in g.players:
            q.s["hp"] = 1100
        g.players[g.subject].s["hp"] = 300
        assert g.advance() == 16, "clock ranks living mechs by HP"
        # The dummy process deliberately sends old output and misses a decision.
        code = """import json,sys,time
for line in sys.stdin:
 o=json.loads(line)
 if o["type"]=="hello": print('{"type":"ready"}',flush=True)
 elif o["type"]=="end": break
 else:
  if o["tick"]==2: time.sleep(.075)
  if o["tick"]==6: sys.exit(0)
  print(json.dumps({"tick":o["tick"],"move":[1,0],"fire":[True,False]}),flush=True)
"""
        client = Client([sys.executable, "-c", code], hello)
        assert client.act({"tick": 0, "type": "obs"})["fire"][0]
        assert client.act({"tick": 2, "type": "obs"}) == {"move": [1, 0]}
        assert client.act({"tick": 4, "type": "obs"})["tick"] == 4
        assert client.act({"tick": 6, "type": "obs"}) == {}
        client.end(16, 0)
        assert client.late >= 1 and client.bad >= 1
        repair = """import json,sys,time
for row in sys.stdin:
 o=json.loads(row)
 if o["type"]=="hello": print('{"type":"ready"}',flush=True)
 elif o["type"]=="end": break
 else:
  if o["tick"]==0:
   print("x"*300000,flush=True)
  print(json.dumps({"tick":o["tick"]}),flush=True)
  if o["tick"]==0: time.sleep(.09)
"""
        client = Client([sys.executable, "-c", repair], hello)
        assert client.act({"type": "obs", "tick": 0}) == {"tick": 0} and client.bad == 1
        assert client.act({"type": "obs", "tick": 2, "padding": "x" * 500000}) == {}
        assert client.partial_input and not client.dead
        import time

        time.sleep(0.07)
        assert client.act({"type": "obs", "tick": 4}) == {"tick": 4}
        client.end(1, 0)
        print("PASS recovery after oversized output and a partially delivered observation")
        print(
            "PASS spawn, schedule, upgrades, contested capture, four weapon kinds, reload, same-tick and clock placement, timeout/coast, stale tick, crash/idle"
        )


if __name__ == "__main__":
    run()
