#!/usr/bin/env python3
"""Repeat the seven single-switch comparisons or a constrained full-covariance CMA-ES study."""
import argparse
import ast
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
SWITCH_NAMES = ("weapon", "route", "quiet", "sight", "loot", "fire", "endgame")
FIELDS = ("practice", "strong", "qualifiers")
BOUNDS = ((.22,.41), (5,45), (.05,2.2), (115,320), (.42,1.25), (35,155))


def replace_value(source, variable, value):
    lines = source.splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith(variable + " = ")]
    if len(matches) != 1:
        raise ValueError(f"expected one module-level {variable} assignment")
    lines[matches[0]] = f"{variable} = {value!r}\n"
    return "".join(lines)


class Study:
    def __init__(self, args):
        self.args = args
        self.revision = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", args.revision], text=True
        ).strip()
        self.directory = args.output.resolve() / self.revision
        self.directory.mkdir(parents=True, exist_ok=True)
        self.files = {name: subprocess.check_output(
            ["git", "-C", str(ROOT), "show", f"{self.revision}:{name}"]
        ) for name in ("bot.py", "arena.py", "Dockerfile", ".dockerignore")}
        source = self.files["bot.py"].decode()
        node = next(s for s in ast.parse(source).body if isinstance(s, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "TUNING" for t in s.targets))
        self.defaults = ast.literal_eval(node.value)

    def evaluate(self, name, split, switches, params):
        folder = self.directory / name
        folder.mkdir(exist_ok=True)
        source = replace_value(self.files["bot.py"].decode(), "SWITCHES", tuple(switches))
        source = replace_value(source, "TUNING", tuple(round(float(x), 5) for x in params))
        (folder / "bot.py").write_text(source)
        for key, value in self.files.items():
            if key != "bot.py":
                (folder / key).write_bytes(value)
        report = folder / "results.json"
        command = [sys.executable, str(ROOT / "localboard.py"), "--native", "--split", split,
                   "--pool", "all", "--workers", str(self.args.workers), "--candidate-dir",
                   str(folder), "--report", str(report)]
        with (folder / "match.log").open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=True)
        scores = json.loads(report.read_text())
        change = [scores[field][split]["paired_gain_ci95"][0] for field in FIELDS]
        print(name, *(f"{field} {v:+.3f}" for field,v in zip(FIELDS, change)), flush=True)
        return change

    def screen(self):
        for name in (*SWITCH_NAMES, "all"):
            enabled = [name == "all" or name == switch for switch in SWITCH_NAMES]
            self.evaluate(name, "p7screen", enabled, self.defaults)

    def tune(self):
        import numpy as np

        rng = np.random.default_rng(70119)
        low, high = np.array(BOUNDS).T
        center = (np.array(self.defaults) - low) / (high - low)
        ndim = len(center)
        population = 8
        weights = np.log(population/2 + .5) - np.log(np.arange(1, population//2 + 1))
        weights /= weights.sum()
        effective = 1 / (weights @ weights)
        cc = (4 + effective/ndim) / (ndim + 4 + 2*effective/ndim)
        cs = (effective + 2) / (ndim + effective + 5)
        c1 = 2 / ((ndim + 1.3)**2 + effective)
        cmu = min(1-c1, 2*(effective - 2 + 1/effective) / ((ndim+2)**2 + effective))
        damping = 1 + 2*max(0, math.sqrt((effective-1)/(ndim+1))-1) + cs
        chi = math.sqrt(ndim) * (1-1/(4*ndim)+1/(21*ndim**2))
        sigma, covariance = .24, np.eye(ndim)
        path_cov, path_sigma = np.zeros(ndim), np.zeros(ndim)
        history = []
        for generation in range(self.args.generations):
            eig, basis = np.linalg.eigh(covariance)
            scales = np.sqrt(np.maximum(eig, 1e-12))
            inverse = (basis / scales) @ basis.T
            raw = center + sigma*((rng.normal(size=(population,ndim))*scales) @ basis.T)
            proposals = 1 - abs(np.mod(raw, 2) - 1)
            utilities = []
            for index, x in enumerate(proposals):
                name = f"generation-{generation}-{index}"
                parameters = low + x*(high-low)
                gain = self.evaluate(name, "p7cma", [True]*7, parameters)
                practice, strong, qualifiers = gain
                utilities.append((practice >= 0, strong+qualifiers if practice >= 0 else practice,
                                  strong+qualifiers))
                history.append(dict(name=name, parameters=parameters.tolist(), gains=gain))
                (self.directory / "search.json").write_text(json.dumps(history, indent=2))
            order = sorted(range(population), key=utilities.__getitem__, reverse=True)[:len(weights)]
            steps = (proposals[order] - center) / sigma
            step = weights @ steps
            center += sigma*step
            path_sigma = (1-cs)*path_sigma + math.sqrt(cs*(2-cs)*effective)*(inverse @ step)
            significant = np.linalg.norm(path_sigma)/math.sqrt(1-(1-cs)**(2*(generation+1))) < (1.4+2/(ndim+1))*chi
            path_cov = (1-cc)*path_cov + significant*math.sqrt(cc*(2-cc)*effective)*step
            covariance = ((1-c1-cmu+(1-significant)*c1*cc*(2-cc))*covariance
                          + c1*np.outer(path_cov,path_cov) + cmu*((steps.T*weights) @ steps))
            sigma *= math.exp(cs/damping * (np.linalg.norm(path_sigma)/chi-1))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("screen","tune"))
    parser.add_argument("--revision", default="6d576d5", help="Git revision containing seven switches")
    parser.add_argument("--output", type=Path, default=ROOT.parent / "runs/study")
    parser.add_argument("--workers", type=int, default=min(48,len(os.sched_getaffinity(0))))
    parser.add_argument("--generations", type=int, default=5)
    args = parser.parse_args()
    study = Study(args)
    getattr(study, args.mode)()
