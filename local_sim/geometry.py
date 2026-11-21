from array import array
import ctypes as ct
import json
import math
from pathlib import Path
from arena import Arena


class Map:
    def __init__(self, root, config, library, arena_class=Arena):
        self.arena = a = arena_class(root, config["mech"])
        self.surfaces = [i for i in range(len(a.links)) if a.links[i]]
        self.data = world = json.loads((Path(root) / "v2/world.json").read_bytes())
        self.bounds = world["bounds"]
        crowns = array("f", [0]) * (a.w * a.h)
        trees = world["trees"]
        names = trees["fields"]
        for idx in range(0, len(trees["data"]), len(names)):
            t = dict(zip(names, trees["data"][idx : idx + len(names)]))
            r = t["crownR"]
            for i in range(-math.ceil(r / 2), math.ceil(r / 2) + 1):
                for j in range(-math.ceil(r / 2), math.ceil(r / 2) + 1):
                    if 4 * (i * i + j * j) <= r * r:
                        cell = a.cell(t["x"] + 2 * i, t["y"] + 2 * j)
                        if cell is not None:
                            crowns[cell] = max(crowns[cell], t["z"] + t["height"])
        self.arrays = [a.floor, a.bottom, a.top, crowns]
        lib = ct.CDLL(str(library))
        ptr = ct.POINTER(ct.c_float)
        lib.set_grid.argtypes = [ct.c_int, ct.c_int] + [ct.c_double] * 3 + [ptr] * 4
        lib.set_grid(
            a.w, a.h, a.x0, a.y0, a.spacing, *(ct.cast(v.buffer_info()[0], ptr) for v in self.arrays)
        )
        self.block = lib.first_block
        self.block.argtypes = [ct.c_double] * 6 + [ct.c_int]
        self.block.restype = ct.c_double
        self._lib = lib

    def clear(self, p, q, canopy=False):
        return self.block(*p, q[0] - p[0], q[1] - p[1], q[2] - p[2], canopy) > 1

    def location(self, rng, roof=False):
        a = self.arena
        if roof:
            for _ in range(64):
                c = rng.randrange(a.w * a.h)
                if a.top[c] > a.floor[c] + 2 and a.top[c] < a.floor[c] + 56:
                    return [a.x0 + (c % a.w) * a.spacing, a.y0 + (c // a.w) * a.spacing, a.top[c]]
        n = rng.choice(self.surfaces)
        return [*a.points[n], a.floor[a.cells[n]]]

    def floor_at(self, x, y):
        c = self.arena.cell(x, y)
        return self.arena.floor[c] if c is not None else 0

    def surface(self, x, y, feet, radius, height, step):
        a = self.arena
        c = a.cell(x, y)
        if c is None:
            return None
        floor = a.floor[c]
        if floor > feet + step:
            return None
        if feet < floor + 2 and a.clearance[c] + 0.4 < radius:
            return None
        if a.top[c] <= feet + step:
            return max(a.top[c], floor)
        if a.bottom[c] < max(feet, floor) + height:
            return None
        return floor
