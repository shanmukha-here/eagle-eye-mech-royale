"""Check the style detector and the two endpoint policies using an unpacked official kit."""

import argparse
import gzip
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

from bot import Bot, OpponentEvidence

ROOT = Path(__file__).resolve().parent


def check_signals(hello, first):
    def frame(t=12, actors=(), beams=(), events=(), sounds=()):
        o = dict(
            first,
            t=t,
            mechs=list(actors),
            beams=list(beams),
            events=list(events),
            sounds=list(sounds),
        )
        o["self"] = dict(first["self"], pos=[0.0, 0.0, 0.0])
        o["zone"] = dict(first["zone"], center=[0.0, 0.0], radius=300)
        return o

    def actor(i, x, hp):
        return dict(
            id=i, kind="mech", pos=[x, 60.0, 0.0], aim=[0.0, 0.0], hp=hp, cubes=0
        )

    config = hello["config"]
    enemy_ids = [i for i in range(16) if i != first["self"]["id"]]
    a, v = actor(enemy_ids[0], 10, 1400), actor(enemy_ids[1], 55, 1400)
    beam = dict(by=a["id"], path=[[10.0, 60.0, 4.5], [55.0, 60.0, 4.5]])
    death = dict(kind="death")
    e = OpponentEvidence(config)
    assert e.witness(frame(sounds=[death]), 1) is None
    assert e.witness(frame(12.4, sounds=[death]), 1) is None
    assert e.witness(frame(240, sounds=[death]), 4) is None
    assert e.witness(frame(241, sounds=[death]), 4) is None
    assert e.witness(frame(242, [a, v], [beam]), 4) == "beam_and_elimination"
    assert e.counts["beam_between_others"] == 1
    e = OpponentEvidence(config)
    assert e.witness(frame(10, [a, v]), 0) is None
    assert (
        e.witness(
            frame(
                10.1, [a, dict(v, hp=1200)], events=[dict(type="hit", target=v["id"])]
            ),
            0,
        )
        is None
    )
    assert "uncredited_hp_drop" not in e.counts
    assert e.witness(frame(11, [a, v]), 0) is None
    assert e.witness(frame(11.1, [a, dict(v, hp=1280)]), 0) is None
    assert e.counts["opposed_hp_drop"] == 1
    e.witness(
        frame(
            11.2,
            [a, v],
            [dict(beam, path=[[10.0, 60.0, 4.5], [0.0, 0.0, 4.5], [55.0, 60.0, 4.5]])],
        ),
        0,
    )
    assert "beam_between_others" not in e.counts
    b = Bot(hello)
    kill = dict(type="kill", target=v["id"])
    assert b.outside_eliminations(dict(first, alive=15, events=[kill])) == 0
    assert b.outside_eliminations(dict(first, alive=14, events=[kill])) == 1
    print(
        "PASS own kills, zone-only deaths, beam intersection, uncredited HP and support without elimination"
    )


def check_transition(kit, hello, original):
    import copy

    spec = importlib.util.spec_from_file_location(
        "organizer_checks", kit / "check/check.py"
    )
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    schema = checker.Schema(kit / "mechfight.schema.json")
    observation = copy.deepcopy(original)
    own_id = observation["self"]["id"]
    ids = [i for i in range(16) if i != own_id]
    x, y, z = observation["self"]["pos"]
    actors = [
        dict(
            id=idx,
            kind="mech",
            pos=[x + dx, y + 60, z],
            vel=[0.0, 0.0, 0.0],
            aim=[0.0, 0.0],
            cubes=0,
            hp=1400,
            max_hp=1400,
            weapons=["gun"],
            charging=False,
        )
        for idx, dx in zip(ids, [10, 55])
    ]
    path = [[a["pos"][0], a["pos"][1], z + 4.5] for a in actors]
    observation.update(alive=12, mechs=actors, events=[], sounds=[], beams=[])
    bot = Bot(hello)
    for j, t in enumerate((120.0, 120.4, 120.5)):
        observation.update(t=t, tick=round(t * 20))
        observation["beams"] = (
            [dict(by=ids[0], kind="laser", age=0.0, path=path)] if j == 1 else []
        )
        assert not list(schema.errors(observation, "Observation"))
        act = bot.act(observation)
        assert not list(schema.errors(json.loads(json.dumps(act)), "Action"))
        assert bot.conservative == (j >= 1)
    assert bot.counts["switch_beam_and_elimination"] == 1
    assert bot.counts["switch_decisecond"] == 1204
    print("PASS schema-valid switch, retained state and one permanent transition")


def check_actions(kit, stream, practice_revision):
    with gzip.open(stream, "rt") as fp:
        hello = json.loads(next(fp))
        obs = [row for line in fp if (row := json.loads(line))["type"] == "obs"]
    hello["map"]["dir"] = str(kit / "arena")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "practice.py"
        path.write_bytes(
            subprocess.check_output(
                ["git", "-C", str(ROOT), "show", practice_revision + ":bot.py"]
            )
        )
        spec = importlib.util.spec_from_file_location("practice_reference", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        practiced = module.Bot(hello)
        hybrid = Bot(hello)
        hybrid.evidence.witness = lambda *_: None
        path = Path(directory) / "phase7.py"
        path.write_bytes(
            subprocess.check_output(["git", "-C", str(ROOT), "show", "04cd061:bot.py"])
        )
        spec = importlib.util.spec_from_file_location("conservative_reference", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        conservative = module.Bot(hello)
        switched = Bot(hello)
        switched.conservative = True
        for o in obs:
            assert hybrid.act(o) == practiced.act(o), ("default", o["tick"])
            assert switched.act(o) == conservative.act(o), ("switched", o["tick"])
    print(f"PASS {len(obs)} identical actions in each fixed style on {stream.name}")
    return hello, obs[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kit", type=Path, default=ROOT.parent / "brief/kit/mechfight-kit"
    )
    parser.add_argument("--stream", type=Path, nargs="*")
    parser.add_argument("--practice-revision", default="7b2492c")
    args = parser.parse_args()
    kit = args.kit.resolve()
    streams = args.stream or [kit / "check/stream.jsonl.gz"]
    for i, s in enumerate(streams):
        h, o = check_actions(kit, s, args.practice_revision)
        if i == 0:
            check_signals(h, o)
            check_transition(kit, h, o)
