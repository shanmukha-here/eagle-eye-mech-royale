"""Traversable surfaces and incremental route planning in the mechfight arena."""
from array import array
from collections import deque
import heapq
import json
import math
from pathlib import Path
import sys


class Arena:
    spacing = 2
    stride = 3

    def __init__(self, directory, mech):
        root = Path(directory) / "v2"
        world = json.loads((root / "world.json").read_bytes())
        meta = world["terrain"]
        heights = array("h")
        heights.frombytes((root / meta["file"]).read_bytes())
        if sys.byteorder != "little":
            heights.byteswap()
        self.x0, self.y0 = meta["minX"], meta["minY"]
        source_width, source_height = meta["nx"], meta["ny"]
        if len(heights) != source_width * source_height:
            raise ValueError("terrain dimensions do not match height data")
        spacing = self.spacing
        self.w = 1 + int((source_width - 1) * meta["step"] / spacing)
        self.h = 1 + int((source_height - 1) * meta["step"] / spacing)
        size = self.w * self.h
        self.floor = array("f")
        # In the official grid the 2 m mesh samples land exactly on source vertices.
        cols = [round(x * spacing / meta["step"]) for x in range(self.w)]
        for j in range(self.h):
            row = round(j * spacing / meta["step"]) * source_width
            self.floor.extend(heights[row + i] * meta["scale"] for i in cols)
        self.bottom = array("f", [math.inf]) * size
        self.top = array("f", [-math.inf]) * size
        for prism in world["prisms"]:
            self._rasterize(prism)
        self.base_height = mech["height"]
        self.base_radius = mech["radius"]
        self._build_clearance(mech)
        self._build_graph()

    def _rasterize(self, prism):
        # Even/odd scan conversion covers the exterior ring and subtracts holes.
        rows = {}
        for ring in [prism["ring"], *prism.get("holes", ())]:
            points = list(zip(ring[::2], ring[1::2]))
            for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
                # Keep thin solids and exact boundary rows in the collision raster.
                dx, dy = x2-x1, y2-y1
                steps = max(1, math.ceil(math.hypot(dx, dy) / (0.45*self.spacing)))
                for k in range(steps+1):
                    c = self.cell(x1+dx*k/steps, y1+dy*k/steps)
                    if c is not None:
                        self.bottom[c] = min(self.bottom[c], prism["base"])
                        self.top[c] = max(self.top[c], prism["top"])
                if y2 < y1:
                    x1, x2, y1, y2 = x2, x1, y2, y1
                if y1 == y2:
                    continue
                start = max(0, math.ceil((y1 - self.y0) / self.spacing))
                stop = min(self.h, math.ceil((y2 - self.y0) / self.spacing))
                slope = (x2 - x1) / (y2 - y1)
                for j in range(start, stop):
                    x = x1 + (self.y0 + j * self.spacing - y1) * slope
                    rows.setdefault(j, []).append(x)
        for j, cuts in rows.items():
            cuts.sort()
            for left, right in zip(cuts[::2], cuts[1::2]):
                lo = max(0, math.ceil((left - self.x0) / self.spacing))
                hi = min(self.w, math.ceil((right - self.x0) / self.spacing))
                for cell in range(j * self.w + lo, j * self.w + hi):
                    self.bottom[cell] = min(self.bottom[cell], prism["base"])
                    self.top[cell] = max(self.top[cell], prism["top"])

    def _build_clearance(self, mech):
        # Chamfer distance in approximate meters (2 m axial, 3 m diagonal).
        w, h = self.w, self.h
        dist = bytearray([240]) * (w * h)
        max_delta = self.spacing * math.tan(math.radians(mech["max_slope_deg"]))
        for j in range(h):
            for i in range(w):
                n = j * w + i
                z = self.floor[n]
                if (i == 0 or j == 0 or i == w - 1 or j == h - 1
                        or (self.top[n] > z + mech["step_up"] and self.bottom[n] < z + mech["height"])
                        or abs(z - self.floor[n - 1]) > max_delta
                        or abs(z - self.floor[n - w]) > max_delta):
                    dist[n] = 0
                else:
                    dist[n] = min(240, dist[n-1]+2, dist[n-w]+2,
                                  dist[n-w-1]+3, dist[n-w+1]+3)
        for j in range(h - 2, 0, -1):
            for n in range((j + 1) * w - 2, j * w, -1):
                dist[n] = min(dist[n], dist[n+1]+2, dist[n+w]+2,
                              dist[n+w-1]+3, dist[n+w+1]+3)
        self.clearance = dist

    def cell(self, x, y):
        i = round((x - self.x0) / self.spacing)
        j = round((y - self.y0) / self.spacing)
        if 0 <= i < self.w and 0 <= j < self.h:
            return j * self.w + i
        return None

    def _build_graph(self):
        s, w = self.stride, self.w
        self.gw = (w - 1) // s + 1
        self.gh = (self.h - 1) // s + 1
        self.points = []
        self.cells = []
        for j in range(self.gh):
            for i in range(self.gw):
                self.points.append((self.x0 + i*s*self.spacing, self.y0 + j*s*self.spacing))
                self.cells.append(j*s*w + i*s)
        self.links = [[] for _ in self.points]
        self.components = [-1] * len(self.points)
        required = self.base_radius + 2.1
        for n, c in enumerate(self.cells):
            if self.clearance[c] < required:
                continue
            i, j = n % self.gw, n // self.gw
            for di, dj in ((1, 0), (0, 1), (1, 1), (-1, 1)):
                if not (0 <= i+di < self.gw and j+dj < self.gh):
                    continue
                other = n + dj*self.gw + di
                delta = dj*w + di
                room = min(self.clearance[c+k*delta] for k in range(s+1))
                if room < required:
                    continue
                self.links[n].append((other, room))
                self.links[other].append((n, room))
        for first, links in enumerate(self.links):
            if not links or self.components[first] >= 0:
                continue
            self.components[first] = first
            pending = deque([first])
            while pending:
                here = pending.popleft()
                for neighbor, _ in self.links[here]:
                    if self.components[neighbor] < 0:
                        self.components[neighbor] = first
                        pending.append(neighbor)

    def nearest(self, xy, component=None, room=0):
        x, y = xy[:2]
        step = self.spacing * self.stride
        gx = min(self.gw-1, max(0, round((x-self.x0)/step)))
        gy = min(self.gh-1, max(0, round((y-self.y0)/step)))
        for r in range(25):
            best, distance = None, math.inf
            for j in range(max(0, gy-r), min(self.gh, gy+r+1)):
                lo, hi = max(0, gx-r), min(self.gw-1, gx+r)
                columns = range(lo, hi+1) if abs(j-gy) == r else (lo, hi)
                for i in columns:
                    n = j*self.gw+i
                    if self.clearance[self.cells[n]] < room or not self.links[n]:
                        continue
                    if component is not None and self.components[n] != component:
                        continue
                    px, py = self.points[n]
                    d = (px-x)**2 + (py-y)**2
                    if d < distance:
                        best, distance = n, d
            if best is not None:
                return best
        return None

    def line_open(self, a, b):
        # Sample game collision volumes along the segment. Foliage does not stop a shot.
        dx, dy, dz = (v-u for u, v in zip(a, b))
        count = max(1, math.ceil(math.hypot(dx, dy) / self.spacing))
        for k in range(1, count+1):
            t = k/count
            c = self.cell(a[0]+dx*t, a[1]+dy*t)
            z = a[2]+dz*t
            if c is None or self.floor[c] > z or self.bottom[c] < z < self.top[c]:
                return False
        return True

    def travel_room(self, pos, direction, length, stats):
        """Walkable distance along one local command, including rooftop departures."""
        x, y, z = pos
        dx, dy = direction
        start = self.cell(x, y)
        if start is None:
            return 0
        elevated = z > self.floor[start] + 2.5
        radius = stats["radius"]
        required = radius + 1.75
        start_room = self.clearance[start]
        for k in range(1, math.ceil(length / self.spacing)+1):
            c = self.cell(x+dx*k*self.spacing, y+dy*k*self.spacing)
            if c is None:
                return (k-1)*self.spacing
            foot = max(z, self.floor[c]) if elevated else self.floor[c]
            solid = self.bottom[c] < foot + stats["height"] and self.top[c] > foot + stats["step_up"]
            # Let a spawn beside a wall walk out of the inflated boundary.
            if solid or (not elevated and self.clearance[c] < required and self.clearance[c] <= start_room):
                return (k-1)*self.spacing
        return length


class Navigator:
    def __init__(self, arena):
        self.arena = arena
        self.goal = None
        self.route = deque()
        self.frontier = []
        self.cost = {}
        self.parent = {}
        self.key = None
        self.last_start = None
        self.last_goal_xy = None
        self.exposure = None
        self.cover_cost = 0.0

    def waypoint(self, pos, dest, radius):
        a = self.arena
        room = max(a.base_radius, radius) + 2.1
        start = a.nearest(pos, room=room)
        if start is None:
            return dest
        component = a.components[start]
        if (self.last_goal_xy is None or math.dist(dest, self.last_goal_xy) > 3
                or self.goal is None or a.components[self.goal] != component
                or a.clearance[a.cells[self.goal]] < room):
            self.goal = a.nearest(dest, component, room)
            self.last_goal_xy = dest
        goal = self.goal
        if goal is None:
            return dest
        key = (goal, math.ceil(room))
        displaced = self.route and math.dist(pos[:2], a.points[self.route[0]]) > 21
        if self.key != key or displaced or (not self.frontier and not self.route and start != self.last_start):
            self.key = key
            self.last_start = start
            self.route.clear()
            self.cost = {start: 0}
            self.parent = {start: None}
            self.frontier = [(math.dist(a.points[start], a.points[goal]), 0, start)]
        for _ in range(400):
            if not self.frontier:
                break
            _, cost, n = heapq.heappop(self.frontier)
            if cost != self.cost[n]:
                continue
            if n == goal:
                while n is not None:
                    self.route.appendleft(n)
                    n = self.parent[n]
                self.frontier.clear()
                break
            x, y = a.points[n]
            for neighbor, clearance in a.links[n]:
                if clearance < room:
                    continue
                nx, ny = a.points[neighbor]
                distance = (8.486 if x != nx and y != ny else 6.0)
                new = cost + distance + max(0, room + 2 - clearance)*0.5
                if self.exposure is not None:
                    new += distance * self.cover_cost * (1 - min(8, self.exposure.cover[neighbor])/8)
                if new < self.cost.get(neighbor, math.inf):
                    self.cost[neighbor] = new
                    self.parent[neighbor] = n
                    heuristic = math.dist(a.points[neighbor], a.points[goal])
                    heapq.heappush(self.frontier, (new + heuristic*1.08, new, neighbor))
        while self.route and math.dist(pos[:2], a.points[self.route[0]]) < 3.2:
            self.route.popleft()
        if self.route:
            return a.points[self.route[0]]
        if start == goal and math.dist(a.points[goal], dest) > 8:
            return a.points[goal]
        return dest


class Exposure:
    """Six-metre cells describing cover, foliage and possible roof landings."""
    def __init__(self, arena, directory):
        self.arena = arena
        world = json.loads((Path(directory) / "v2/world.json").read_bytes())
        trees = world["trees"]
        self.crowns = {}
        self.crown_bases = {}
        names = trees["fields"]
        for start in range(0, len(trees["data"]), len(names)):
            tree = dict(zip(names, trees["data"][start:start+len(names)]))
            r = tree["crownR"]
            x, y = tree["crownX"], tree["crownY"]
            for j in range(math.floor((y-r-arena.y0)/6), math.ceil((y+r-arena.y0)/6)+1):
                for i in range(math.floor((x-r-arena.x0)/6), math.ceil((x+r-arena.x0)/6)+1):
                    if (0 <= i < arena.gw and 0 <= j < arena.gh and
                        math.hypot(arena.x0+6*i-x, arena.y0+6*j-y) <= r):
                        node = j*arena.gw+i
                        self.crown_bases[node] = min(self.crown_bases.get(node, math.inf), tree["z"] + tree["crownBase"])
                        self.crowns[node] = max(self.crowns.get(node, -math.inf), tree["z"]+tree["height"])
        self.cover = bytearray(len(arena.cells))
        self.roofs = []
        for n, cell in enumerate(arena.cells):
            i, j = n % arena.gw, n // arena.gw
            ground = arena.floor[cell]
            if 4 < arena.top[cell]-ground < 32:
                self.roofs.append(n)
            if not arena.links[n]:
                continue
            shield = 0
            for di, dj in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(-1,1),(1,-1),(-1,-1)):
                for r in (1, 3, 6):
                    col, row = i + di*r, j + dj*r
                    if not (0 <= col < arena.gw and 0 <= row < arena.gh):
                        break
                    other = arena.cells[row*arena.gw+col]
                    if arena.floor[other] > ground+6 or arena.bottom[other] < ground+8 < arena.top[other]:
                        shield += 1
                        break
            self.cover[n] = shield + (2 if self.crowns.get(n, -math.inf) > ground+6 else 0)

    def node(self, x, y):
        a = self.arena
        i = min(a.gw-1, max(0, round((x-a.x0)/6)))
        j = min(a.gh-1, max(0, round((y-a.y0)/6)))
        return j*a.gw+i

    def screened(self, source, dest):
        if not self.arena.line_open(source, dest):
            return True
        length = math.dist(source[:2], dest[:2])
        if length <= 45:
            return False
        n = max(1, math.ceil(length/4))
        foliage = 0
        for i in range(1, n):
            t = i/n
            x, y, z = (u+t*(v-u) for u,v in zip(source, dest))
            node = self.node(x,y)
            if self.crown_bases.get(node, math.inf) <= z < self.crowns.get(node, -math.inf):
                foliage += length/n
                if foliage >= 8:
                    return True
        return False
