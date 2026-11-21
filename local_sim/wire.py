"""Deadline-aware mechfight/1 child process transport for the local match runner."""

import json
import math
import os
import resource
import select
import subprocess
import time


def child_limits():
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def valid_action(a, tick):
    if not isinstance(a, dict) or type(a.get("tick")) not in (int, float) or a["tick"] != tick:
        return False

    def number(v):
        return type(v) in (float, int) and math.isfinite(v)

    for k in ("move", "aim", "fire"):
        if k in a and (
            not isinstance(a[k], list)
            or len(a[k]) != 2
            or not all(type(v) is bool if k == "fire" else number(v) for v in a[k])
        ):
            return False
    return all(isinstance(a[k], bool) for k in ("boost", "reload", "jump") if k in a) and all(
        number(a[k]) for k in ("take", "drop", "pick") if k in a
    )


class Client:
    def __init__(self, cmd, hello):
        self.dead = False
        self.buf = b""
        self.late = 0
        self.bad = 0
        self.missed = 0
        self.last = {}
        self.max_line = hello["config"]["bot"]["max_line_bytes"]
        self.budget = hello["config"]["bot"]["tick_budget_ms"] / 1000
        self.coast = hello["config"]["bot"]["coast_s"]
        start = time.monotonic()
        if os.path.basename(cmd[0]) == "docker":
            cmd = cmd[:2] + ["--cpuset-cpus", str(min(os.sched_getaffinity(0)))] + cmd[2:]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            bufsize=0,
            stderr=subprocess.DEVNULL,
            preexec_fn=None if os.path.basename(cmd[0]) == "docker" else child_limits,
        )
        os.set_blocking(self.proc.stdin.fileno(), False)
        os.set_blocking(self.proc.stdout.fileno(), False)
        stop = start + hello["config"]["bot"]["hello_budget_ms"] / 1000
        self._write(hello, stop)
        ready = self._line(stop)
        if not isinstance(ready, dict) or ready.get("type") != "ready":
            self.dead = True
            self.close()

    def _write(self, m, stop):
        raw = json.dumps(m, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        data = memoryview(raw)
        while data and not self.dead:
            try:
                n = os.write(self.proc.stdin.fileno(), data)
                data = data[n:]
            except BlockingIOError:
                if not select.select([], [self.proc.stdin], [], max(0, stop - time.monotonic()))[1]:
                    # A partially written line cannot be reused as a new message.
                    if len(data) != len(raw):
                        self.dead = True
                    return False
            except BrokenPipeError:
                self.dead = True
        return not self.dead

    def _line(self, stop):
        while not self.dead and time.monotonic() < stop:
            i = self.buf.find(b"\n")
            if i >= 0:
                raw, self.buf = self.buf[:i], self.buf[i + 1 :]
                if len(raw) <= self.max_line:
                    try:
                        return json.loads(raw)
                    except (ValueError, UnicodeError):
                        pass
                self.bad += 1
                continue
            if not select.select([self.proc.stdout], [], [], max(0, stop - time.monotonic()))[0]:
                break
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                self.dead = True
                break
            self.buf += chunk
            if len(self.buf) > 2 * self.max_line and b"\n" not in self.buf:
                self.dead = True
        return None

    def act(self, o):
        if self.dead:
            return {}
        stop = time.monotonic() + self.budget
        if self._write(o, stop):
            while time.monotonic() < stop:
                a = self._line(stop)
                if a is None:
                    break
                if valid_action(a, o["tick"]) and time.monotonic() <= stop:
                    self.missed = 0
                    self.last = {k: a[k] for k in ("move", "boost", "aim") if k in a}
                    return a
                self.bad += 1
        self.late += 1
        self.missed += 1
        return self.last if not self.dead and self.missed * 0.1 <= self.coast else {}

    def end(self, place, kills):
        try:
            if not self.dead:
                self._write({"type": "end", "place": place, "kills": kills}, time.monotonic() + 0.5)
            self.proc.stdin.close()
            self.proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            self.close()

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
