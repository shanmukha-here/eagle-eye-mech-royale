"""Headless mechfight/1 rules model. All randomness belongs to a match or actor."""

import math
import random

DT = 0.05
TAU = math.tau


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def unit(dx, dy):
    d = math.hypot(dx, dy)
    return (dx / d, dy / d) if d else (0, 0)


def angle(x):
    return (x + math.pi) % TAU - math.pi


def new_weapon(kind, cfg):
    s = dict(cfg["weapons"][kind])
    w = {"kind": kind, "stats": s, "cooldown": 0}
    if kind == "gun":
        w.update(ammo=int(s["mag"]), reload=0)
    if kind == "laser":
        w["charge"] = 0
    return w


class Player:
    def __init__(self, i, pos, cfg, rng, role):
        stats = dict(cfg["mech"])
        self.s = {
            "id": i,
            "pos": pos,
            "vel": [0.0, 0.0, 0.0],
            "aim": [rng.uniform(-math.pi, math.pi), 0.0],
            "hp": stats["hp"],
            "max_hp": stats["hp"],
            "energy": stats["energy"],
            "max_energy": stats["energy"],
            "grounded": True,
            "in_zone": True,
            "cubes": 0,
            "upgrades": [],
            "weapons": [new_weapon("gun", cfg), None],
            "stats": stats,
        }
        self.role = role
        self.rng = rng
        self.id = i
        self.action = {}
        self.events = []
        self.memory = {}
        self.offer = None
        self.pending = 0
        self.last_combat = -100
        self.spent = -100
        self.silenced = -100
        self.life = True
        self.kills = 0
        self.last_step = -10
        self.crate_hits = 0
        self.drops = 0
        self.hit_by = "none"
        self.fatal_source = None
        self.fatal_weapon = None
        self.controller = None

    def actor(self):
        s = self.s
        return {k: s[k] for k in ("id", "pos", "vel", "aim", "hp", "max_hp", "cubes")} | {
            "kind": "mech",
            "weapons": [w["kind"] for w in s["weapons"] if w],
            "charging": any(w and w.get("charge", 0) > 0 for w in s["weapons"]),
        }


class Match:
    def __init__(self, geo, hello, seed, roles, ffa=False):
        self.geo = geo
        self.cfg = c = hello["config"]
        self.rng = r = random.Random(seed)
        self.ffa = ffa
        self.tick = 0
        self.t = 0
        self.subject = r.randrange(16)
        ids = [i for i in range(16) if i != self.subject]
        r.shuffle(ids)
        self.order = ids
        self.players = [None] * 16
        self.zone_phases = []
        center = c["zone"]["start_center"]
        radius = c["zone"]["start_radius"]
        t = 0
        for ph in c["zone"]["phases"]:
            theta = r.uniform(0, TAU)
            offset = r.uniform(0.1, 0.95) * (radius - ph["radius"])
            nxt = [center[0] + offset * math.cos(theta), center[1] + offset * math.sin(theta)]
            bounds = geo.bounds
            nxt = [
                clamp(nxt[0], bounds["minX"] + 20, bounds["maxX"] - 20),
                clamp(nxt[1], bounds["minY"] + 20, bounds["maxY"] - 20),
            ]
            self.zone_phases.append(
                (
                    t + ph["hold_s"],
                    t + ph["hold_s"] + ph["shrink_s"],
                    center,
                    radius,
                    nxt,
                    ph["radius"],
                    ph["dps"],
                )
            )
            t = self.zone_phases[-1][1]
            center, radius = nxt, ph["radius"]
        self.phase = 0
        self.update_zone()
        spawns = []
        for i in range(16):
            best, spacing = None, -1
            for _ in range(700):
                pos = geo.location(r)
                if (
                    math.dist(pos[:2], self.zone["center"])
                    > self.zone["radius"] * c["match"]["spawn_zone_fraction"]
                ):
                    continue
                dist = min((math.dist(pos[:2], q[:2]) for q in spawns), default=10000)
                if dist > spacing:
                    best, spacing = pos, dist
                if dist >= c["match"]["spawn_min_separation"]:
                    break
            spawns.append(best)
        # Preserve 4/4/4/3 tier order while varying opponents within each tier.
        roles = list(roles)
        rr = random.Random(seed ^ 0x5CE1)
        for lo, hi in ((0, 4), (4, 8), (8, 12), (12, 15)):
            tier = roles[lo:hi]
            rr.shuffle(tier)
            roles[lo:hi] = tier
        for i, pos in enumerate(spawns):
            role = "subject" if i == self.subject else roles[ids.index(i)]
            self.players[i] = Player(i, pos, c, random.Random(seed * 37 + i), role)
        self.crates = {
            1000 + i: {
                "id": 1000 + i,
                "pos": geo.location(r, r.random() < c["crates"]["roof_fraction"]),
                "hp": c["crates"]["hp"],
            }
            for i in range(int(c["crates"]["count"]))
        }
        self.pickups = {}
        self.ground_guns = {}
        self.cubes = {}
        self.item_id = 8000
        self.cube_id = 5000
        self.bullets = []
        self.bullet_id = 0
        self.sounds = []
        self.beams = []
        for _ in range(int(c["loot"]["floor_count"])):
            self.spawn_weapon(geo.location(r, r.random() < c["loot"]["floor_roof_fraction"]))
        self.npcs = {}
        self.home = {}
        self.npc_cd = {}
        self.aggro = {}
        for i in range(100, 100 + int(c["npc"]["count"])):
            pos = geo.location(r)
            self.home[i] = pos[:]
            self.npcs[i] = {
                "id": i,
                "kind": "npc",
                "pos": pos,
                "vel": [0, 0, 0],
                "aim": [0.0, 0.0],
                "hp": c["npc"]["hp"],
                "max_hp": c["npc"]["hp"],
            }
            self.npc_cd[i] = r.random()
        self.drop_plan = []
        self.drops = {}
        for i, call in enumerate(c["drops"]["times"]):
            ph = next(p for p in self.zone_phases if p[1] >= call)
            p = geo.location(r)
            for _ in range(2000):
                if math.dist(p[:2], ph[4]) < ph[5]:
                    break
                p = geo.location(r)
            self.drop_plan.append((call, 20000 + i, p))
        self.team_seen = -100
        self.shared_pos = None
        self.active = set()

    def spawn_weapon(self, pos, kind=None):
        if kind is None:
            kind = self.rng.choice(("mortar", "laser", "lightning"))
        self.pickups[self.item_id] = {"id": self.item_id, "pos": list(pos), "kind": kind}
        self.item_id += 1
        return self.item_id - 1

    def drop_slot(self, player, slot):
        held = player.s["weapons"][slot]
        item_id = self.spawn_weapon(player.s["pos"], held["kind"])
        if held["kind"] == "gun":
            self.ground_guns[item_id] = (held["ammo"], held["reload"], held["cooldown"])
        player.s["weapons"][slot] = None
        player.events.append({"type": "dropped", "id": item_id, "slot": slot, "kind": held["kind"]})

    def spawn_cubes(self, pos, n):
        for _ in range(n):
            self.cubes[self.cube_id] = {
                "id": self.cube_id,
                "pos": [pos[0] + self.rng.uniform(-1, 1), pos[1] + self.rng.uniform(-1, 1), pos[2]],
            }
            self.cube_id += 1

    def update_zone(self):
        while self.phase + 1 < len(self.zone_phases) and self.t >= self.zone_phases[self.phase][1]:
            self.phase += 1
        start, end, c, r, n, nr, dps = self.zone_phases[self.phase]
        f = clamp((self.t - start) / (end - start), 0, 1)
        self.zone = {
            "center": [c[0] + (n[0] - c[0]) * f, c[1] + (n[1] - c[1]) * f],
            "radius": r + (nr - r) * f,
            "next_center": n,
            "next_radius": nr,
            "shrink_start": start,
            "shrink_end": end,
            "dps": dps,
        }

    def visible(self, p, pos, height=1):
        s = p.s
        x, y, z = s["pos"]
        dx, dy = pos[0] - x, pos[1] - y
        d2 = dx * dx + dy * dy
        v = self.cfg["vision"]
        near = v["near_range"] + s["stats"]["near_range_bonus"]
        if d2 > (v["range"] + s["stats"]["vision_bonus"]) ** 2:
            return False
        far = d2 > near * near
        if far and abs(angle(math.atan2(dy, dx) - s["aim"][0])) > math.radians(v["fov_deg"] * 0.5):
            return False
        eye = (x, y, z + s["stats"]["eye_height"])
        return self.geo.clear(eye, (pos[0], pos[1], pos[2] + height * 0.5), far) or self.geo.clear(
            eye, (pos[0], pos[1], pos[2] + height), far
        )

    def refresh_team(self):
        alive = [i for i in self.order if self.players[i].life]
        dead = 15 - len(alive)
        limit = 15 if self.phase >= 4 else 1 + dead // 2 + self.phase
        self.active = set(alive[:limit])
        goal = self.players[self.subject].s["pos"]
        if any(self.visible(self.players[i], goal, 9) for i in alive):
            self.team_seen = self.t
            self.shared_pos = goal[:]

    def obs(self, p):
        s = p.s
        others = [
            q
            for q in self.players
            if q is not p and q.life and (self.ffa or p.id == self.subject or q.id == self.subject)
        ]
        actors = []
        for q in others:
            if not self.ffa and p.id != self.subject and p.id not in self.active:
                continue
            shared = (
                not self.ffa
                and p.id != self.subject
                and self.t - self.team_seen < self.cfg["vision"]["memory_s"]
            )
            if shared or self.visible(p, q.s["pos"], q.s["stats"]["height"]):
                p.memory[q.id] = self.t
            if self.t - p.memory.get(q.id, -100) <= self.cfg["vision"]["memory_s"]:
                actors.append(q.actor())
        for i, n in self.npcs.items():
            if self.visible(p, n["pos"], self.cfg["npc"]["height"]):
                p.memory[i] = self.t
            if self.t - p.memory.get(i, -100) <= self.cfg["vision"]["memory_s"]:
                actors.append(n)

        def items(source, h):
            return [v for v in source.values() if self.visible(p, v["pos"], h)]

        sounds = []
        for kind, by, point, when in self.sounds:
            if by == p.id or self.t - when > 0.101:
                continue
            d = math.dist(point, s["pos"])
            if d > self.cfg["sound"]["range"].get(kind, 0):
                continue
            yaw = math.atan2(point[1] - s["pos"][1], point[0] - s["pos"][0])
            noise = p.rng.gauss(
                0, math.radians(self.cfg["sound"]["bearing_noise_deg"]) * s["stats"]["hearing"]
            )
            sounds.append(
                {
                    "kind": kind,
                    "bearing": angle(yaw + noise),
                    "dist": "near" if d < 60 else "mid" if d < 160 else "far",
                    "age": self.t - when,
                }
            )
        beams = [
            {"by": i, "kind": kind, "path": path, "age": self.t - when}
            for i, kind, path, when in self.beams
            if i != p.id
            and self.t - when <= 0.101
            and (self.visible(p, path[0]) or self.visible(p, path[-1]))
        ]
        bullets = [
            {"id": b[0], "kind": b[1], "pos": b[3], "vel": b[4]}
            for b in self.bullets
            if b[2] != p.id and self.visible(p, b[3])
        ]
        return {
            "type": "obs",
            "tick": self.tick,
            "t": self.t,
            "alive": sum(q.life for q in self.players),
            "self": s,
            "mechs": actors,
            "projectiles": bullets,
            "crates": items(self.crates, 2.4),
            "cubes": items(self.cubes, 1),
            "pickups": items(self.pickups, 1),
            "sounds": sounds,
            "beams": beams,
            "events": p.events,
            "drops": list(self.drops.values()),
            "zone": self.zone,
            "offer": p.offer,
        }

    def sound(self, kind, i, pos):
        self.sounds.append((kind, i, tuple(pos), self.t))

    def move(self, p):
        s, a = p.s, p.action
        stats = s["stats"]
        pos, vel = s["pos"], s["vel"]
        old = s["aim"]
        yaw, pitch = a.get("aim", old)
        old[0] = angle(
            old[0] + clamp(angle(yaw - old[0]), -stats["turn_rate"] * DT, stats["turn_rate"] * DT)
        )
        old[1] += clamp(
            clamp(pitch, stats["min_pitch"], stats["max_pitch"]) - old[1],
            -stats["pitch_rate"] * DT,
            stats["pitch_rate"] * DT,
        )
        mx, my = a.get("move", (0, 0))
        norm = max(1, math.hypot(mx, my))
        grounded = s["grounded"]
        if grounded and a.get("jump"):
            vel[2] = stats["jump_speed"]
            grounded = False
        boost = a.get("boost", False) and s["energy"] > stats["jet_drain"] * DT
        speed = stats["boost_speed"] if boost and grounded else stats["walk_speed"]
        if any(w and w.get("charge", 0) > 0 for w in s["weapons"]):
            speed *= stats["charge_move_mult"]
        ax, ay = speed * mx / norm - vel[0], speed * my / norm - vel[1]
        f = min(
            1, DT * stats["ground_accel" if grounded else "air_accel"] / max(0.001, math.hypot(ax, ay))
        )
        vel[0] += ax * f
        vel[1] += ay * f
        if boost and (not grounded or mx or my):
            p.spent = self.t
            s["energy"] = max(0, s["energy"] - DT * stats["boost_drain" if grounded else "jet_drain"])
        elif self.t - p.spent >= stats["energy_regen_delay"]:
            s["energy"] = min(s["max_energy"], s["energy"] + stats["energy_regen"] * DT)
        if not grounded:
            vel[2] = min(
                stats["max_rise_speed"],
                vel[2] + DT * ((stats["jet_accel"] if boost else 0) - stats["gravity"]),
            )
        z = pos[2]
        for axis in (0, 1):
            nextxy = pos[:2]
            nextxy[axis] += vel[axis] * DT
            support = self.geo.surface(*nextxy, z, stats["radius"], stats["height"], stats["step_up"])
            if support is not None:
                pos[axis] = nextxy[axis]
                if grounded and support < z - stats["step_up"]:
                    grounded = False
                if grounded:
                    z = support
            else:
                vel[axis] = 0
        c = self.geo.arena.cell(pos[0], pos[1])
        if c is not None:
            floor = self.geo.arena.floor[c]
            top = self.geo.arena.top[c]
            base = self.geo.arena.bottom[c]
            if top <= z + stats["step_up"]:
                floor = max(floor, top)
            nz = z + vel[2] * DT
            if vel[2] > 0 and z + stats["height"] <= base < nz + stats["height"]:
                nz = base - stats["height"]
                vel[2] = 0
            if nz <= floor + 0.001:
                nz = floor
                grounded = True
                vel[2] = 0
            pos[2] = nz
        s["grounded"] = grounded
        if math.hypot(vel[0], vel[1]) > 5 and self.t - p.last_step >= self.cfg["sound"]["step_every"]:
            self.sound("boost" if boost and grounded else "step" if grounded else "jet", p.id, pos)
            p.last_step = self.t
        s["in_zone"] = math.dist(pos[:2], self.zone["center"]) <= self.zone["radius"]
        if not s["in_zone"]:
            amount = self.zone["dps"] * DT
            s["hp"] -= amount
            p.last_combat = self.t
            p.hit_by = "zone"
            if s["hp"] <= 0 and p.fatal_weapon is None:
                p.fatal_weapon = "zone"
            p.events.append(
                {"type": "damaged", "amount": amount, "weapon": "zone", "by": None, "bearing": None}
            )
        elif self.t - p.last_combat >= stats["regen_delay"]:
            s["hp"] = min(s["max_hp"], s["hp"] + stats["regen_per_s"] * DT)

    def hurt(self, src, dst, dmg, kind, origin):
        if dst < 16:
            q = self.players[dst]
            if not q.life or (
                src >= 0 and src < 16 and src != dst and not self.ffa and self.subject not in (src, dst)
            ):
                return
            s = q.s
            yaw = math.atan2(origin[1] - s["pos"][1], origin[0] - s["pos"][0])
            rear = abs(angle(yaw - s["aim"][0])) > math.radians(s["stats"]["front_arc_deg"] * 0.5)
            dmg *= max(0, 1 - s["stats"]["armor"]) * (s["stats"]["rear_mult"] if rear else 1)
            s["hp"] -= dmg
            q.last_combat = self.t
            q.hit_by = kind
            if s["hp"] <= 0 and q.fatal_weapon is None:
                q.fatal_weapon, q.fatal_source = kind, src
            q.events.append(
                {
                    "type": "damaged",
                    "amount": dmg,
                    "weapon": kind,
                    "by": src,
                    "bearing": yaw,
                    "rear": rear,
                }
            )
            if s["hp"] <= 0 and s["hp"] + dmg > 0 and 0 <= src < 16 and src != dst:
                self.players[src].kills += 1
                self.players[src].events.append({"type": "kill", "target": dst})
        elif dst in self.npcs:
            n = self.npcs[dst]
            n["hp"] -= dmg
            self.aggro[dst] = src
            if n["hp"] <= 0:
                self.spawn_cubes(n["pos"], self.cfg["npc"]["cubes"])
                del self.npcs[dst]
        elif dst in self.crates:
            c = self.crates[dst]
            c["hp"] -= dmg
            if c["hp"] <= 0:
                self.spawn_cubes(c["pos"], self.cfg["crates"]["cubes"])
                if self.rng.random() < self.cfg["loot"]["crate_weapon_chance"]:
                    self.spawn_weapon(c["pos"])
                del self.crates[dst]
        if 0 <= src < 16:
            q = self.players[src]
            q.last_combat = self.t
            q.events.append({"type": "hit", "target": dst, "amount": dmg, "weapon": kind})

    def objects(self, src):
        for p in self.players:
            if (
                p.life
                and p.id != src
                and (self.ffa or src >= 100 or src == self.subject or p.id == self.subject)
            ):
                yield p.id, p.s["pos"], p.s["stats"]["radius"], p.s["stats"]["height"]
        if src < 16:
            for i, n in self.npcs.items():
                yield i, n["pos"], self.cfg["npc"]["radius"], self.cfg["npc"]["height"]
            for i, c in self.crates.items():
                yield i, c["pos"], self.cfg["crates"]["radius"], self.cfg["crates"]["height"]

    def ray(self, src, p, delta):
        dx, dy, dz = delta
        limit = min(1.00001, self.geo.block(*p, *delta, 0))
        best = None
        denom = dx * dx + dy * dy
        for i, q, r, h in self.objects(src):
            ox, oy = q[0] - p[0], q[1] - p[1]
            # Intersection of the game cylinder's horizontal disk and vertical interval.
            dot = ox * dx + oy * dy
            disc = dot * dot - denom * (ox * ox + oy * oy - r * r)
            if disc < 0 or denom < 1e-9:
                continue
            root = math.sqrt(disc)
            lo = max(0, (dot - root) / denom)
            hi = min(limit, (dot + root) / denom)
            if abs(dz) > 1e-9:
                a, b = (q[2] - p[2]) / dz, (q[2] + h - p[2]) / dz
                lo = max(lo, min(a, b))
                hi = min(hi, max(a, b))
            elif not q[2] <= p[2] <= q[2] + h:
                continue
            if lo <= hi and lo < limit:
                best = i
                limit = lo
        return best, limit

    def projectile(self, kind, src, p, v, damage, ttl, stats):
        self.bullet_id += 1
        self.bullets.append([self.bullet_id, kind, src, list(p), list(v), damage, ttl, stats])

    def fire(self, p):
        s = p.s
        a = p.action
        for j, w in enumerate(s["weapons"]):
            if w is None:
                continue
            st = w["stats"]
            k = w["kind"]
            w["cooldown"] = max(-DT, w["cooldown"] - DT)
            trig = a.get("fire", (False, False))[j]
            if k == "gun":
                if w["reload"] > 0:
                    w["reload"] -= DT
                    if w["reload"] <= 1e-8:
                        w["reload"] = 0
                        w["ammo"] = int(st["mag"])
                    continue
                if (a.get("reload") or w["ammo"] <= 0) and w["ammo"] < st["mag"]:
                    w["reload"] = st["reload"]
                    continue
            if w["cooldown"] > 1e-8:
                continue
            frac = 1
            if k == "laser":
                if p.silenced > self.t:
                    w["charge"] = 0
                    continue
                if trig and w["charge"] < st["charge"] + st["hold_s"]:
                    w["charge"] += DT
                    continue
                if w["charge"] <= 0:
                    continue
                frac = st["min_frac"] + (1 - st["min_frac"]) * min(
                    1, max(st["min_charge"], w["charge"]) / st["charge"]
                )
                w["charge"] = 0
            elif not trig:
                continue
            yaw, pitch = s["aim"]
            if k == "gun":
                w["ammo"] -= 1
                yaw += p.rng.gauss(0, math.radians(st["spread_deg"]))
                pitch += p.rng.gauss(0, math.radians(st["spread_deg"]))
            if k == "lightning":
                if s["energy"] < st["energy"]:
                    continue
                s["energy"] -= st["energy"]
                p.spent = self.t
            w["cooldown"] = st["cooldown"] + min(0, w["cooldown"]) if trig else st["cooldown"]
            origin = [
                *s["pos"][:2],
                s["pos"][2]
                + (s["stats"]["height"] + 0.5 if k == "mortar" else s["stats"]["muzzle_height"]),
            ]
            aim = [math.cos(pitch) * math.cos(yaw), math.cos(pitch) * math.sin(yaw), math.sin(pitch)]
            shift = s["stats"]["radius"] + 0.5
            delta = [d * shift for d in aim]
            f = min(1, self.geo.block(*origin, *delta, 0))
            muzzle = [x + d * f for x, d in zip(origin, delta)]
            dmg = (
                st["damage"]
                * (
                    1
                    + self.cfg["cubes"]["damage_per_cube"]
                    * min(s["cubes"], self.cfg["cubes"]["stat_cap"])
                )
                * frac
            )
            self.sound(k, p.id, muzzle)
            if k in ("gun", "mortar"):
                ttl = st["ttl"] if k == "mortar" else st["range"] / st["speed"]
                self.projectile(k, p.id, muzzle, [v * st["speed"] for v in aim], dmg, ttl, st)
            elif k == "laser":
                delta = [d * st["range"] for d in aim]
                hit, f = self.ray(p.id, muzzle, delta)
                end = [x + d * min(1, f) for x, d in zip(muzzle, delta)]
                self.beams.append((p.id, k, [muzzle, end], self.t))
                if hit is not None:
                    if hit < 16 and any(
                        w and w["kind"] == "mortar" for w in self.players[hit].s["weapons"]
                    ):
                        dmg *= 1 + st["cookoff"]
                    self.hurt(p.id, hit, dmg, k, muzzle)
            else:
                path = [muzzle]
                seen = set()
                for jump in range(int(st["chain"]) + 1):
                    limit = st["range"] if jump == 0 else st["chain_range"]
                    best = None
                    for i, q, _, h in self.objects(p.id):
                        if i in seen:
                            continue
                        mid = (q[0], q[1], q[2] + h / 2)
                        d = math.dist(path[-1], mid)
                        if d > limit:
                            continue
                        if jump == 0 and abs(
                            angle(math.atan2(q[1] - muzzle[1], q[0] - muzzle[0]) - yaw)
                        ) > math.radians(st["cone_deg"]):
                            continue
                        if self.geo.clear(path[-1], mid):
                            best = (i, mid)
                            limit = d
                    if best is None:
                        break
                    i, mid = best
                    if i < 16:
                        self.players[i].silenced = self.t + st["short_s"]
                    self.hurt(p.id, i, dmg * st["chain_falloff"] ** jump, k, path[-1])
                    path.append(mid)
                    seen.add(i)
                if len(path) > 1:
                    self.beams.append((p.id, k, path, self.t))

    def fly(self):
        remain = []
        for b in self.bullets:
            _, k, src, p, v, dmg, ttl, st = b
            delta = [x * DT for x in v]
            if k == "mortar":
                v[2] -= st["gravity"] * DT
            hit, f = self.ray(src, p, delta)
            q = [x + d * min(f, 1) for x, d in zip(p, delta)]
            if hit is not None:
                self.hurt(src, hit, dmg, k, p)
            if hit is None and f > 1 and ttl > DT:
                b[3] = q
                b[6] -= DT
                remain.append(b)
                continue
            if k == "mortar":
                self.sound("explosion", src, q)
                for pl in self.players:
                    if not pl.life:
                        continue
                    pt = pl.s["pos"]
                    d = math.dist(pt[:2], q[:2])
                    if (
                        d > st["splash_radius"]
                        or abs(pt[2] - q[2]) > pl.s["stats"]["height"] + st["splash_radius"]
                    ):
                        continue
                    if not self.geo.clear((q[0], q[1], q[2] + 1), (pt[0], pt[1], pt[2] + 4.5)):
                        continue
                    if not self.ffa and src != pl.id and self.subject not in (src, pl.id):
                        continue
                    fall = 1 - (1 - st["splash_edge"]) * d / st["splash_radius"]
                    self.hurt(src, pl.id, st["splash"] * fall * (0.5 if src == pl.id else 1), k, q)
                    pl.s["energy"] = max(0, pl.s["energy"] - st["drain"] * fall)
                    nx, ny = unit(pt[0] - q[0], pt[1] - q[1])
                    vel = pl.s["vel"]
                    vel[0] += nx * st["knock"] * fall
                    vel[1] += ny * st["knock"] * fall
                    vel[2] += st["knock_up"] * fall
                    pl.s["grounded"] = False
        self.bullets = remain

    def gain(self, p, n):
        s = p.s
        c = self.cfg["cubes"]
        before = s["cubes"]
        s["cubes"] += n
        mult = 1 + c["hp_per_cube"] * min(s["cubes"], c["stat_cap"])
        hp = s["stats"]["hp"] * mult
        s["hp"] += hp - s["max_hp"]
        s["max_hp"] = hp
        for k in ("height", "radius", "muzzle_height", "eye_height"):
            s["stats"][k] = self.cfg["mech"][k] * (1 + c["size_per_cube"] * s["cubes"])
        p.pending += sum(before < x <= s["cubes"] for x in c["upgrade_thresholds"])
        p.events.append({"type": "pickup", "cubes": s["cubes"]})

    def offer(self, p):
        s = p.s
        if p.offer:
            pick = p.action.get("pick")
            if pick is None and self.t >= p.offer["expires"]:
                pick = 0
            if isinstance(pick, int) and 0 <= pick < len(p.offer["choices"]):
                up = p.offer["choices"][pick]["id"]
                spec = next(u for u in self.cfg["upgrades"] if u["id"] == up)
                for ef in spec["effects"]:
                    path = ef["path"].split(".")
                    if path[0] == "mech":
                        target = s["stats"]
                    else:
                        w = next((w for w in s["weapons"] if w and w["kind"] == path[1]), None)
                        if w is None:
                            continue
                        target = w["stats"]
                    k = path[-1]
                    target[k] = target[k] * ef.get("mul", 1) + ef.get("add", 0)
                s["upgrades"].append(up)
                p.offer = None
                s["max_energy"] = s["stats"]["energy"]
                hp = s["stats"]["hp"] * (
                    1 + min(s["cubes"], self.cfg["cubes"]["stat_cap"]) * self.cfg["cubes"]["hp_per_cube"]
                )
                s["hp"] += hp - s["max_hp"]
                s["max_hp"] = hp
                p.events.append({"type": "upgrade", "id": up})
        if not p.offer and p.pending:
            held = {w["kind"] for w in s["weapons"] if w}
            choices = [
                u
                for u in self.cfg["upgrades"]
                if s["upgrades"].count(u["id"]) < u["max"]
                and ("requires" not in u or u["requires"] in held)
            ]
            if choices:
                opts = p.rng.sample(choices, min(3, len(choices)))
                p.offer = {
                    "id": len(s["upgrades"]) + 1,
                    "expires": self.t + self.cfg["bot"]["offer_timeout_s"],
                    "choices": [{k: u[k] for k in ("id", "name", "desc")} for u in opts],
                }
            p.pending -= 1

    def items(self, alive):
        # Resolve simultaneous pickup claims by distance, then slot.
        pending = {}
        for p in alive:
            s = p.s
            pos = s["pos"]
            pick = p.action.get("take")
            if pick in self.pickups:
                item = self.pickups[pick]
                dist = math.dist(pos[:2], item["pos"][:2])
                if dist <= self.cfg["loot"]["pickup_radius"] and abs(pos[2] - item["pos"][2]) <= 4:
                    pending.setdefault(pick, []).append((dist, p.id))
            if pick is None and p.action.get("drop") in (0, 1):
                slot = int(p.action["drop"])
                held = s["weapons"][slot]
                if held and not held.get("charge", 0):
                    self.drop_slot(p, slot)
            self.offer(p)
        for i, requests in pending.items():
            for _, pid in sorted(requests):
                p = self.players[pid]
                slots = p.s["weapons"]
                item = self.pickups[i]
                if any(w and w["kind"] == item["kind"] for w in slots):
                    continue
                slot = p.action.get("drop")
                if slot is None and None in slots:
                    slot = slots.index(None)
                if slot not in (0, 1):
                    continue
                slot = int(slot)
                if slots[slot] and slots[slot].get("charge", 0) > 0:
                    continue
                if slots[slot]:
                    self.drop_slot(p, slot)
                w = new_weapon(item["kind"], self.cfg)
                slots[slot] = w
                if i in self.ground_guns:
                    w["ammo"], w["reload"], w["cooldown"] = self.ground_guns.pop(i)
                w["cooldown"] = max(w["cooldown"], w["stats"]["cooldown"], self.cfg["loot"]["equip_s"])
                del self.pickups[i]
                p.events.append({"type": "took", "kind": w["kind"], "slot": slot, "id": i})
                break
        for i, cube in list(self.cubes.items()):
            q = cube["pos"]
            best = None
            for p in alive:
                pt = p.s["pos"]
                if abs(q[0] - pt[0]) > 3.5 or abs(q[2] - pt[2]) > 4:
                    continue
                d = math.hypot(q[0] - pt[0], q[1] - pt[1])
                if d <= self.cfg["cubes"]["pickup_radius"] and (best is None or (d, p.id) < best):
                    best = (d, p.id)
            if best is not None:
                self.gain(self.players[best[1]], 1)
                del self.cubes[i]
        dc = self.cfg["drops"]
        while self.drop_plan and self.t >= self.drop_plan[0][0]:
            call, i, pt = self.drop_plan.pop(0)
            self.drops[i] = {
                "id": i,
                "pos": pt,
                "radius": dc["radius"],
                "capture_s": dc["capture_s"],
                "progress": 0,
                "lands_in": dc["fall_s"],
                "holder": None,
            }
        for i, d in list(self.drops.items()):
            if d["lands_in"] > 0:
                d["lands_in"] = max(0, d["lands_in"] - DT)
                continue
            inside = [p for p in alive if math.dist(p.s["pos"][:2], d["pos"][:2]) <= d["radius"]]
            if len(inside) > 1:
                continue
            if not inside:
                d.update(holder=None, progress=0)
                continue
            p = inside[0]
            if d["holder"] != p.id:
                d.update(holder=p.id, progress=0)
            d["progress"] += DT
            if d["progress"] >= d["capture_s"] - 1e-8:
                self.gain(p, dc["cubes"])
                p.s["hp"] = p.s["max_hp"]
                p.s["energy"] = p.s["max_energy"]
                p.drops += 1
                p.events.append({"type": "drop", "id": i, "cubes": p.s["cubes"]})
                del self.drops[i]

    def drones(self, alive):
        cfg = self.cfg["npc"]
        for i, n in self.npcs.items():
            pos = n["pos"]
            home = self.home[i]
            target = None
            previous = self.aggro.get(i, -1)
            if 0 <= previous < 16 and self.players[previous].life:
                q = self.players[previous]
                if (
                    math.dist(pos, q.s["pos"]) < cfg["deaggro_range"]
                    and math.dist(pos, home) < cfg["chase_leash"]
                ):
                    target = q
            if target is None and self.tick % 6 == 0 and math.dist(pos, home) <= cfg["leash"]:
                for q in alive:
                    pt = q.s["pos"]
                    if math.dist(pos, pt) <= cfg["aggro_range"] and self.geo.clear(
                        (*pos[:2], pos[2] + 2), (*pt[:2], pt[2] + 4)
                    ):
                        target = q
                        self.aggro[i] = q.id
                        break
            if target is None:
                self.aggro.pop(i, None)
                dest = [
                    home[0] + 14 * math.sin(i + self.t * 0.11),
                    home[1] + 14 * math.cos(i + self.t * 0.09),
                ]
            else:
                dest = target.s["pos"]
            ux, uy = unit(dest[0] - pos[0], dest[1] - pos[1])
            nx = pos[0] + ux * cfg["speed"] * DT
            ny = pos[1] + uy * cfg["speed"] * DT
            h = self.geo.surface(nx, ny, pos[2], cfg["radius"], cfg["height"], 1.2)
            if h is not None:
                pos[:] = [nx, ny, h]
                n["vel"] = [ux * cfg["speed"], uy * cfg["speed"], 0]
            if target is None or self.t < self.npc_cd[i]:
                continue
            origin = [*pos[:2], pos[2] + 2]
            pt = [*dest[:2], dest[2] + 4.5]
            if not self.geo.clear(origin, pt):
                continue
            self.npc_cd[i] = self.t + cfg["cooldown"]
            yaw = math.atan2(pt[1] - pos[1], pt[0] - pos[0]) + self.rng.gauss(
                0, math.radians(cfg["spread_deg"])
            )
            pitch = math.atan2(pt[2] - origin[2], math.dist(pt[:2], pos[:2]))
            speed = cfg["projectile_speed"]
            self.projectile(
                "npc",
                i,
                origin,
                [
                    speed * math.cos(yaw) * math.cos(pitch),
                    speed * math.sin(yaw) * math.cos(pitch),
                    speed * math.sin(pitch),
                ],
                cfg["damage"],
                1.5,
                {},
            )
            n["aim"] = [yaw, pitch]
            self.sound("npc_gun", i, pos)

    def advance(self):
        self.update_zone()
        alive = [p for p in self.players if p.life]
        for p in alive:
            self.move(p)
        for p in alive:
            self.fire(p)
        # Resolve overlaps in horizontal game cylinders after movement.
        for ix, q in enumerate(alive):
            a = q.s
            pa = a["pos"]
            for other in alive[ix + 1 :]:
                b = other.s
                pb = b["pos"]
                dx, dy = pb[0] - pa[0], pb[1] - pa[1]
                radii = a["stats"]["radius"] + b["stats"]["radius"]
                if (
                    abs(dx) >= radii
                    or abs(dy) >= radii
                    or pa[2] + a["stats"]["height"] <= pb[2]
                    or pb[2] + b["stats"]["height"] <= pa[2]
                ):
                    continue
                d = math.hypot(dx, dy)
                if d >= radii:
                    continue
                ux, uy = unit(dx, dy) if d > 1e-6 else (1, 0)
                shift = (radii - d) * 0.5
                for s, sign in ((a, -1), (b, 1)):
                    pt = s["pos"]
                    nx = pt[0] + sign * ux * shift
                    ny = pt[1] + sign * uy * shift
                    if (
                        self.geo.surface(
                            nx,
                            ny,
                            pt[2],
                            s["stats"]["radius"],
                            s["stats"]["height"],
                            s["stats"]["step_up"],
                        )
                        is not None
                    ):
                        pt[0], pt[1] = nx, ny
        self.drones(alive)
        self.fly()
        died = [p for p in alive if p.s["hp"] <= 0]
        for p in died:
            p.life = False
            self.spawn_cubes(p.s["pos"], p.s["cubes"] + self.cfg["cubes"]["death_drop_extra"])
            for w in p.s["weapons"]:
                if w and w["kind"] != "gun":
                    self.spawn_weapon(p.s["pos"], w["kind"])
            self.sound("death", p.id, p.s["pos"])
        survivors = [p for p in alive if p.life]
        if not self.players[self.subject].life:
            return len(survivors) + 1
        if len(survivors) == 1:
            return 1
        self.items(survivors)
        self.tick += 1
        self.t = self.tick * DT
        self.sounds = [v for v in self.sounds if self.t - v[-1] < 0.101]
        self.beams = [v for v in self.beams if self.t - v[-1] < 0.101]
        if self.t >= self.cfg["match"]["duration_s"]:
            return 1 + sum(p.s["hp"] > self.players[self.subject].s["hp"] for p in survivors)
        return None
