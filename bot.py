"""Eagle Eye entry for the mechfight/1 JSON-lines game protocol."""
import json
import math
import os
import select

from arena import Arena, Navigator


def direction(dx, dy):
    norm = math.hypot(dx, dy)
    return (dx/norm, dy/norm) if norm > 0.01 else (0.0, 0.0)


def angle_delta(a, b):
    return (a-b+math.pi) % math.tau-math.pi


class Bot:
    def __init__(self, hello):
        self.cfg = hello["config"]
        self.arena = Arena(hello["map"]["dir"], self.cfg["mech"])
        self.nav = Navigator(self.arena)
        self.seed_angle = math.tau * ((hello["seed"] % 10007) / 10007)
        self.surv = False
        self.avoiding = False
        self.drones = {}
        self.previous_target = None
        self.charge_since = {}
        self.searched = set()
        self.last_search_sample = -2
        self.loot = {}
        self.plan_at = -1
        self.goal = (0, 0)
        self.mode = ""
        self.encounter = None
        self.encounter_until = -1
        self.mech_kills = 0
        self.uncredited_eliminations = 0
        self.last_contact = -100
        self.last_drone = (-1, -100)
        self.damage_window = []
        self.recover_until = -100
        self.other_fight = (frozenset(), -100)
        self.last_bearing = 0
        self.last_probe = None
        self.sprinting = False
        self.shortcut_until = -1
        self.jet_until = -1
        self.jet_height = 0
        self.unstick_until = -1
        self.rescue_until = -1
        self.blocked_count = 0
        self.escape_contact = None
        self.escape_seen = -100
        self.next_exit = -1
        self.exit_point = None
        self.previous_move = (0, 0)

    def _resources(self, obs):
        me = obs["self"]
        pos = me["pos"]
        t = obs["t"]
        visible = set()
        for key, value in (("cubes", "cube"), ("pickups", "weapon"), ("crates", "crate")):
            for item in obs[key]:
                item_id = item["id"]
                visible.add(item_id)
                self.loot[item_id] = (item["pos"], item.get("kind", value), t)
        for m in obs["mechs"]:
            if m["kind"] == "npc":
                self.drones[m["id"]] = (m["pos"], t)
        self.drones = {k: v for k, v in self.drones.items() if t - v[1] < 30}
        for item_id, (point, _, seen) in list(self.loot.items()):
            if t-seen > (48 if _ in ("laser","lightning") else 60 if _ == "crate" and self.uncredited_eliminations >= 1 else 14) or (item_id not in visible and math.dist(point, pos) < 7):
                del self.loot[item_id]

    def _visible_contacts(self, obs):
        me = obs["self"]
        p = me["pos"]
        muzzle = (p[0], p[1], p[2]+me["stats"]["muzzle_height"])
        actors = sorted(obs["mechs"], key=lambda m: math.dist(m["pos"], p))
        threats = []
        npcs = []
        for m in actors:
            pm = m["pos"]
            center = (pm[0], pm[1], pm[2]+self._actor_center(m))
            if m["kind"] == "mech":
                threats.append((m, math.dist(p, pm), self.arena.line_open(muzzle, center)))
            elif len(npcs) < 2 and math.dist(p, pm) < 50 and self.arena.line_open(muzzle, center):
                npcs.append(m)
        return threats, npcs

    def _actor_center(self, actor):
        if actor["kind"] == "npc":
            return self.cfg["npc"]["height"] * 0.5
        scale = 1 + self.cfg["cubes"]["size_per_cube"] * actor.get("cubes", 0)
        return self.cfg["mech"]["height"] * 0.5 * scale

    def _choose_goal(self, obs, threats, pressured, urgent):
        me, zone = obs["self"], obs["zone"]
        pos = me["pos"]
        x, y = pos[:2]
        cx, cy = zone["next_center"]
        radius = zone["next_radius"]
        if radius == 0 and zone["radius"] <= 5:
            self.goal, self.mode = (cx, cy), "zone"
            return
        safe_radius = max(0, radius - min(23, radius*0.22))
        escape = pressured and any(line for _, _, line in threats)
        retreat = escape and not urgent
        closest = threats[0][0]["pos"] if threats else pos
        goals = []

        def add(px, py, mode, bonus, must_fit=False, detour=False):
            gap = math.hypot(px-cx, py-cy)-safe_radius
            if must_fit and gap > 0:
                return
            distance = math.hypot(px-x, py-y)
            penalty = max(0, gap)*1.4
            actual_gap = math.hypot(px-cx, py-cy)-radius
            if detour and actual_gap <= 0:
                penalty *= 0.3
            if detour and actual_gap > 0:
                # During a hold we can collect nearby loot before moving inward.
                spare = zone["shrink_start"] - obs["t"]
                travel = (distance+gap)*1.5 / me["stats"]["walk_speed"]
                if (travel+6 > spare or
                        math.dist(zone["center"], (px, py)) > zone["radius"]-4):
                    return
                penalty = 0
            cell = self.arena.cell(px, py)
            if cell is None or self.arena.clearance[cell] < me["stats"]["radius"] + 2.1:
                return
            score = bonus - distance*0.22 - penalty
            room = self.arena.clearance[cell]
            score += max(0, 1-abs(room-10)/18)*5
            if retreat:
                point = (px, py, self.arena.floor[cell]+me["stats"]["muzzle_height"])
                blocked = False
                for m, _, line in threats[:3]:
                    mx, my, mz = m["pos"]
                    if line and not self.arena.line_open((mx, my, mz+self._actor_center(m)), point):
                        score += 25
                        blocked = True
                if blocked:
                    mode = "cover"
                score += min(100, math.hypot(px-closest[0], py-closest[1])) * 0.12
            if self.mode == mode and math.dist((px, py), self.goal) < 9:
                score += 4
            goals.append((score, px, py, mode))

        dx, dy = direction(x-cx, y-cy)
        inset = min(safe_radius*0.76, math.hypot(x-cx, y-cy))
        ax, ay = cx+dx*inset, cy+dy*inset
        add(ax, ay, "zone", 10)
        if safe_radius > 0:
            # Inspect alternatives around the reachable side of the upcoming circle.
            for dist in (min(25, safe_radius), min(65, safe_radius)):
                for i in range(8):
                    angle = self.seed_angle + i*math.pi/4
                    add(ax+dist*math.cos(angle), ay+dist*math.sin(angle), "zone", 10, True)
        if retreat:
            for dist in (14, 30):
                for i in range(8):
                    angle = i*math.pi/4
                    add(x+dist*math.cos(angle), y+dist*math.sin(angle), "cover", 12)
        if (not threats and not urgent and obs["t"]-self.last_contact > 6
                and obs["t"] < 90 and me["hp"] > 0.85*me["max_hp"] and radius >= 140
                and (me["weapons"][1] is None or me["cubes"] < 2)
                and math.hypot(x-cx, y-cy) < safe_radius-45):
            if self.mode == "search" and math.dist(self.goal, pos[:2]) > 7:
                add(*self.goal, "search", 26, True)
            else:
                angle = self.seed_angle + int(obs["t"]/5)*2.40
                for offset in (0, 1.57, -1.57):
                    add(x+36*math.cos(angle+offset), y+36*math.sin(angle+offset), "search", 25, True)
        if not urgent and obs["t"] < 60 and me["weapons"][1] is None:
            for point, kind, seen in self.loot.values():
                if kind in ("laser", "lightning") and abs(point[2] - pos[2]) <= 4 and math.dist(pos, point) < 140:
                    add(*point[:2], kind, 155 - 2 * (obs["t"] - seen), detour=True)
        if not urgent and (me["weapons"][1] is None or me["weapons"][1]["kind"] == "laser"):
            # The destination may be at a different ground elevation from our feet.
            travel_budget = (zone["shrink_end"] - obs["t"] - 8) * me["stats"]["walk_speed"] / 1.65
            for point, kind, seen in self.loot.values():
                if kind != "lightning":
                    continue
                cell = self.arena.cell(*point[:2])
                if cell is None or abs(self.arena.floor[cell] - point[2]) > 4:
                    continue
                if self.arena.clearance[cell] < me["stats"]["radius"] + 2:
                    continue
                distance = math.dist(pos, point)
                if distance > 280:
                    continue
                return_distance = max(0, math.dist(point[:2], zone["next_center"]) - radius + 9)
                if distance + return_distance > travel_budget:
                    continue
                if math.dist(point[:2], zone["center"]) > zone["radius"] - 6:
                    continue
                score = 245 - distance * 0.22 - min(35, (obs["t"] - seen) * 0.4)
                goals.append((score, *point[:2], "lightning"))
        if not urgent and me["weapons"][1] is None and obs["t"] < 45 and not pressured:
            seconds_left = zone["shrink_end"] - obs["t"] - 9
            # Prefer reachable ground we have not passed while looking for a second weapon.
            visited_centers = [((i + 0.5) * 25, (j + 0.5) * 25) for i, j in self.searched]
            # Search farther across open ground to expose more floor pickups.
            for distance in (56, 100, 150):
                for step in range(16):
                    heading = self.seed_angle + step * math.pi / 8
                    px, py = x + distance * math.cos(heading), y + distance * math.sin(heading)
                    cell = self.arena.cell(px, py)
                    if cell is None or self.arena.clearance[cell] < me["stats"]["radius"] + 2:
                        continue
                    if math.dist((px, py), zone["center"]) > zone["radius"] - 9:
                        continue
                    home = max(0, math.dist((px, py), zone["next_center"]) - radius + 10)
                    if (home + distance) * 1.8 / me["stats"]["walk_speed"] > seconds_left:
                        continue
                    novelty = min((math.hypot(px - ex, py - ey) for ex, ey in visited_centers), default=90)
                    keep_goal = max(0, 10 - math.dist((px, py), self.goal) * 0.12) if self.mode == "explore" else 0
                    score = 86 + min(95, novelty) * 0.23 - distance * 0.06
                    score += min(24, self.arena.clearance[cell]) * 0.18 + keep_goal
                    goals.append((score, px, py, "explore"))
        self.farming = (self.uncredited_eliminations >= 1 and not urgent and not threats and obs["t"] - self.last_contact > 4
                        and obs["t"] < 140 and me["hp"] >= 0.7*me["max_hp"] and me["cubes"] < 10)
        if self.farming:
            # Before the first fight, nearby crates and drones are the cheapest cubes,
            # and each early cube raises HP and damage for every later exchange.
            for point, kind, seen in self.loot.values():
                if kind == "crate" and abs(point[2]-pos[2]) < 6 and math.dist(pos, point) < 260:
                    add(*point[:2], "harvest", 150, detour=True)
                if kind == "cube" and abs(point[2]-pos[2]) < 6 and math.dist(pos, point) < 90:
                    add(*point[:2], "cube", 240, detour=True)
            for point, seen in self.drones.values():
                if math.dist(pos[:2], point[:2]) < 260:
                    ux, uy = direction(x-point[0], y-point[1])
                    add(point[0]+ux*55, point[1]+uy*55, "drone", 140, detour=True)
        if not urgent and me["cubes"] < 12:
            for point, kind, seen in self.loot.values():
                if kind == "cube" and math.dist(pos,point) < (33 if not self.uncredited_eliminations >= 1 else (0 if self.surv else 70) if threats and me["hp"] > 0.4*me["max_hp"] else (0 if self.surv else 33) if threats else 160) and abs(point[2]-pos[2]) < 5:
                    add(*point[:2],kind,240,detour=True)
        if not urgent and radius > 7 and (not retreat or
                (len(threats) == 1 and me["hp"] > 0.60*me["max_hp"] and not me["weapons"][1])):
            values = {"cube": 44, "lightning": 37, "laser": 36, "crate": 21}
            gearing = me["weapons"][1] is None and obs["t"] < 120
            if gearing:
                values.update(laser=105, lightning=105, crate=50)
            if me["cubes"] < 3:
                values["cube"] += 27
            for point, kind, seen in self.loot.values():
                if kind not in values or (kind in ("lightning", "laser") and all(me["weapons"])):
                    continue
                if abs(point[2]-pos[2]) > 12 or math.dist(pos, point) > (165 if gearing else 105):
                    continue
                if any(clear and math.dist(m["pos"][:2], point[:2]) < 32
                       for m, _, clear in threats[:3]):
                    continue
                add(point[0], point[1], kind, values[kind]-0.7*(obs["t"]-seen), detour=True)
            for d in obs.get("drops", ()):
                point = d["pos"][:2]
                eta = math.dist(pos[:2], point) * 1.35 / me["stats"]["walk_speed"]
                if d["lands_in"] > eta+3 or d.get("holder") not in (None, me["id"]):
                    continue
                if any(math.dist(m["pos"][:2], point) < d["radius"]+16 for m, _, _ in threats):
                    continue
                missing = max(0, 1-me["hp"]/me["max_hp"])
                bonus = 46 + 70*missing + 3*d["progress"]
                add(*point, "drop", bonus, detour=True)
        if (not urgent and not retreat and obs["alive"] > 2
                and me["hp"] > 0.72*me["max_hp"] and obs["t"]-self.last_drone[1]>1.5):
            for enemy,distance,clear in threats:
                if not clear or not 12 < distance < 155:
                    continue
                enemy_hp = enemy.get("hp", self.cfg["mech"]["hp"])
                confidence = me["hp"]/max(enemy_hp,1)
                crowd = sum(1 for m,_,_ in threats if m["id"] != enemy["id"]
                            and math.dist(m["pos"][:2],enemy["pos"][:2]) < 52)
                endgame = self.uncredited_eliminations >= 1 and not self.surv and enemy_hp < 0.6*enemy.get("max_hp", self.cfg["mech"]["hp"])
                if self.surv and not self._prey(enemy, [m for m, _, _ in threats]):
                    continue
                if (crowd and not endgame) or (confidence < 0.84 and not endgame):
                    continue
                facing = math.atan2(y-enemy["pos"][1], x-enemy["pos"][0])
                looking_away = abs(angle_delta(facing,enemy["aim"][0])) > 1.15
                if not (looking_away or confidence > 1.20 or me["weapons"][1]):
                    continue
                stamina = 15 if enemy["id"] == self.encounter and obs["t"] < self.encounter_until else 0
                bonus = 54 + min(24, max(0,confidence-1)*25) + 14*looking_away + stamina
                bonus += 30*endgame
                px,py = enemy["pos"][:2]
                dx,dy = direction(x-px,y-py)
                spacing = 22 if me["weapons"][1] else 32
                add(px+dx*spacing,py+dy*spacing,"duel",bonus,detour=True)
        if not urgent and radius >= 40 and self.uncredited_eliminations >= 1:
            goals.extend(self._perimeter_goals(obs, threats))
        if goals:
            _, gx, gy, self.mode = max(goals)
            self.goal = (gx, gy)
            if self.mode == "duel":
                enemy = min((m for m,_,clear in threats if clear),
                            key=lambda m:math.dist(m["pos"][:2],self.goal),default=None)
                if enemy:
                    self.encounter, self.encounter_until = enemy["id"], obs["t"]+2.5
        else:
            self.goal, self.mode = (cx, cy), "zone"

    def _perimeter_goals(self, obs, threats):
        """Sample reachable points on the quiet side of the current circle."""
        me, zone = obs["self"], obs["zone"]
        x, y = me["pos"][:2]
        cx, cy = zone["center"]
        nx, ny = zone["next_center"]
        speed = me["stats"]["walk_speed"]
        # Reserve 14 seconds and budget a 1.7x detour for the return journey.
        time_left = max(0, zone["shrink_end"] - obs["t"] - 14)
        slack = min(120, time_left * speed * 0.39)
        ring_radius = max(15, min(zone["radius"] - 22, zone["next_radius"] + slack - 18))
        bearing = math.atan2(y - cy, x - cx)
        for offset in (0, 0.32, -0.32, 0.70, -0.70, 1.15, -1.15):
            angle = bearing + offset
            dx = cx + ring_radius * math.cos(angle) - x
            dy = cy + ring_radius * math.sin(angle) - y
            ux, uy = direction(dx, dy)
            step = min(65, math.hypot(dx, dy))
            for length in (step, step * 0.5):
                px, py = x + ux * length, y + uy * length
                cell = self.arena.cell(px, py)
                if cell is None or self.arena.clearance[cell] < me["stats"]["radius"] + 2.1:
                    continue
                return_distance = max(0, math.hypot(px - nx, py - ny) - zone["next_radius"] + 10)
                if (return_distance + length) * 1.7 / speed > time_left:
                    continue
                radial_distance = math.hypot(px - cx, py - cy)
                if radial_distance > zone["radius"] - 10:
                    continue
                separation = min(
                    (math.dist((px, py), actor["pos"][:2]) for actor, _, _ in threats), default=130
                )
                radial_error = abs(radial_distance - ring_radius)
                score = 78 - min(radial_error, 120) * 0.28 + min(140, separation) * 0.13 - 0.11 * length
                yield score, px, py, "skirt"

    def _can_recover_in_view(self, obs, threats):
        # A visible beam ending on another mech is evidence of a separate fight.
        # Only sit it out while wounded and while both opponents face elsewhere.
        t, me = obs["t"], obs["self"]
        if obs["alive"] <= 3 or me["hp"] >= 0.90*me["max_hp"] or self.damage_window:
            return False
        nearby = [m for m, distance, clear in threats if clear]
        if len(nearby) < 2:
            return False
        for m, distance, clear in threats:
            if not clear:
                continue
            dx, dy = me["pos"][0]-m["pos"][0], me["pos"][1]-m["pos"][1]
            if distance < 55 or abs(angle_delta(math.atan2(dy, dx), m["aim"][0])) < 0.57:
                return False
        ids = {m["id"] for m in nearby}
        for beam in obs["beams"]:
            if beam["by"] not in ids or not beam["path"]:
                continue
            end = beam["path"][-1]
            for m in nearby:
                if (m["id"] != beam["by"] and math.dist(end[:2], m["pos"][:2]) < 6
                        and m["pos"][2]-1 <= end[2] <= m["pos"][2]+2*self._actor_center(m)+1):
                    self.other_fight = (frozenset((beam["by"], m["id"])), t+1.25)
                    return True
        pair, expires = self.other_fight
        return t < expires and pair <= ids

    def _loadout(self, obs, act):
        me = obs["self"]
        if obs["offer"]:
            priorities = {"plating": 10, "armor": 9.5, "gun_damage": 9, "nanites": 8.5,
                          "gun_rate": 8, "gun_mag": 7.5, "servos": 7, "lightning_range": 9.7,
                          "laser_damage": 7, "gun_velocity": 6, "jets": 5, "sensors": 5}
            choices = obs["offer"]["choices"]
            if choices:
                act["pick"] = max(range(len(choices)), key=lambda i: priorities.get(choices[i]["id"], 3))
        if me["weapons"][1] and me["weapons"][1]["kind"] == "laser":
            for item in obs["pickups"]:
                if item["kind"] == "lightning" and math.dist(me["pos"], item["pos"]) < self.cfg["loot"]["pickup_radius"]:
                    act["drop"] = 1
                    act["take"] = item["id"]
                    break
        if any(w is None for w in me["weapons"]):
            p = me["pos"]
            for item in obs["pickups"]:
                s = item["pos"]
                if (item["kind"] in ("laser", "lightning") and abs(s[2]-p[2]) <= 4
                        and math.dist(s[:2], p[:2]) < self.cfg["loot"]["pickup_radius"]):
                    act["take"] = item["id"]
                    break

    def _prey(self, m, mechs):
        # A mech worth fighting mid-game: damaged and with no other mech near it.
        weak = m.get("hp", self.cfg["mech"]["hp"]) < 0.6*m.get("max_hp", self.cfg["mech"]["hp"])
        return weak and not any(q["id"] != m["id"] and math.dist(q["pos"][:2], m["pos"][:2]) < 60 for q in mechs)

    def _target(self, obs, threats, npcs, urgent):
        me = obs["self"]
        live = [(m, d) for m, d, clear in threats if clear]
        if self.mode == "heal":
            return None, 0
        if live:
            def priority(pair):
                m, d = pair
                hp = m.get("hp", self.cfg["mech"]["hp"])
                commitment = 43 if m["id"]==self.encounter and obs["t"]<self.encounter_until else 0
                return (d*0.28 + hp*0.07 - commitment)
            m, _ = min(live, key=priority)
            return m, self._actor_center(m)
        if not urgent:
            for drone in npcs:
                if drone["id"] == self.last_drone[0] and obs["t"]-self.last_drone[1] < 2:
                    return drone, self._actor_center(drone)
        if urgent or me["hp"] < me["max_hp"]*0.68 or obs["t"] - self.last_contact < 3:
            return None, 0
        if npcs:
            return npcs[0], self._actor_center(npcs[0])
        if getattr(self, "farming", False):
            p = me["pos"]
            muzzle = (p[0], p[1], p[2]+me["stats"]["muzzle_height"])
            for m in sorted((m for m in obs["mechs"] if m["kind"] == "npc"), key=lambda m: math.dist(m["pos"], p)):
                if math.dist(m["pos"], p) < 130 and self.arena.line_open(
                        muzzle, (*m["pos"][:2], m["pos"][2]+self._actor_center(m))):
                    return m, self._actor_center(m)
        for item in sorted(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"])):
            p, q = me["pos"], item["pos"]
            h = self.cfg["crates"]["height"]*0.5
            if math.dist(p, q) < (120 if getattr(self, 'farming', False) else 64) and self.arena.line_open(
                    (p[0], p[1], p[2]+me["stats"]["muzzle_height"]), (q[0], q[1], q[2]+h)):
                return item, h
        return None, 0

    def _weapons(self, obs, target, center, act, urgent):
        me = obs["self"]
        gun = me["weapons"][0]
        side = me["weapons"][1]
        if gun and gun["kind"] == "gun":
            ammo = gun.get("ammo", 0)
            if not gun.get("reload", 0) and (ammo == 0 or (target is None and ammo <= gun["stats"]["mag"]*0.72)):
                act["reload"] = True
        if target is None:
            return
        p, q = me["pos"], target["pos"]
        vx, vy, vz = target.get("vel", (0, 0, 0))
        muzzle = (p[0], p[1], p[2]+me["stats"]["muzzle_height"])
        distance = math.dist(p, q)
        laser = side and side["kind"] == "laser"
        charge = side.get("charge", 0) if laser else 0
        # The laser fires on release. Switch to the observed center for its release tick.
        laser_due = laser and charge >= side["stats"]["charge"]-0.095
        seconds = max(0, distance-3)/max(1, gun["stats"]["speed"]) if gun else 0
        if target.get("kind") == "mech":
            if self.previous_target and self.previous_target[0] == target["id"]:
                _, last_time, old_vx, old_vy = self.previous_target
                elapsed = obs["t"] - last_time
                if 0.045 < elapsed < 0.22:
                    horizon = min(seconds, 0.48)
                    vx += max(-28, min(28, (vx - old_vx) / elapsed)) * horizon * 0.52
                    vy += max(-28, min(28, (vy - old_vy) / elapsed)) * horizon * 0.52
            self.previous_target = (target["id"], obs["t"], *target["vel"][:2])
        if laser_due:
            seconds = 0
        point = (q[0]+vx*seconds, q[1]+vy*seconds, q[2]+center+vz*seconds)
        dx, dy, dz = (v-u for u, v in zip(muzzle, point))
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(dz, math.hypot(dx, dy))
        act["aim"] = [yaw, pitch]
        ey = abs(angle_delta(yaw, me["aim"][0]))
        ep = abs(pitch-me["aim"][1])
        angular_radius = math.atan2(1.6, max(1, distance))
        aligned = ey < angular_radius+0.075 and ep < angular_radius+0.055
        valid_line = self.arena.line_open(muzzle, point)
        fire = [bool(gun and aligned and valid_line and distance <= gun["stats"]["range"]+2), False]
        if side and side["kind"] == "lightning":
            stats = side["stats"]
            fire[1] = (distance < stats["range"] and ey < math.radians(stats["cone_deg"])*0.9
                       and me["energy"] >= stats["energy"] + (12 if urgent else 0))
        elif laser:
            if charge > 0:
                fire[1] = not (laser_due and aligned and valid_line)
            else:
                fire[1] = bool(not urgent and valid_line and side["cooldown"] <= 0.1
                               and distance <= side["stats"]["range"] and ey < 0.5)
        act["fire"] = fire

    def _incoming_projectiles(self, obs):
        """Select up to six visible gun rounds passing through the mech's height and footprint."""
        me = obs["self"]
        hazards = []
        for shot in obs["projectiles"]:
            if shot["kind"] != "gun":
                continue
            rx, ry, rz = (a - b for a, b in zip(me["pos"], shot["pos"]))
            vx = shot["vel"][0] - me["vel"][0]
            vy = shot["vel"][1] - me["vel"][1]
            eta = (rx * vx + ry * vy) / max(1, vx * vx + vy * vy)
            if not 0.05 < eta < 0.82:
                continue
            dz = rz + (me["vel"][2] - shot["vel"][2]) * eta
            if not -me["stats"]["height"] - 1.5 < dz < 2:
                continue
            mx, my = rx - vx * eta, ry - vy * eta
            if math.hypot(mx, my) < me["stats"]["radius"] + 4.5:
                hazards.append((eta, mx, my, vx, vy))
                if len(hazards) == 6:
                    break
        return hazards

    def _dodge_score(self, score, me, ax, ay, hazards):
        stats = me["stats"]
        ox = ax * stats["walk_speed"] - me["vel"][0]
        oy = ay * stats["walk_speed"] - me["vel"][1]
        change = math.hypot(ox, oy) / stats["ground_accel" if me["grounded"] else "air_accel"]
        for eta, mx, my, vx, vy in hazards:
            # A direction change takes time. Integrate the achievable velocity change up to the pass.
            drift = eta - 0.5 * change if eta >= change else 0.5 * eta ** 2 / max(0.001, change)
            clearance = abs((mx + ox * drift) * vy - (my + oy * drift) * vx) / max(1, math.hypot(vx, vy))
            fraction = max(0, min(1, clearance / (stats["radius"] + 0.75)))
            score += 14 * fraction / max(0.18, eta)
        return score

    def _move(self, obs, threats, urgent, pressured):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        p = me["pos"]
        goal_dist = math.dist(self.goal, p[:2])
        waypoint = self.nav.waypoint(p, self.goal, me["stats"]["radius"])
        route = direction(waypoint[0]-p[0], waypoint[1]-p[1])
        direct = direction(self.goal[0]-p[0], self.goal[1]-p[1])
        cell = self.arena.cell(p[0], p[1])
        elevated = cell is not None and p[2] > self.arena.floor[cell] + 3
        projected = (self.nav.goal is not None
                     and math.dist(self.goal, self.arena.points[self.nav.goal]) > 15)
        route_length = self.nav.cost.get(self.nav.goal, 0)
        if (route_length > goal_dist*1.55 and goal_dist > 45 and route[0]*direct[0]+route[1]*direct[1] < 0.5
                and route_length/me["stats"]["walk_speed"] > zone["shrink_end"]-t-6):
            # A long ground detour can miss the circle although a short jet crossing fits.
            self.shortcut_until = t + 4
        shortcut = t < self.shortcut_until or projected
        if shortcut:
            stats = me["stats"]
            max_climb = max(0, (me["max_energy"]-6)/stats["jet_drain"]-0.7)*stats["max_rise_speed"]
            for length in range(4, min(56, math.ceil(goal_dist))+1, 4):
                c = self.arena.cell(p[0]+direct[0]*length, p[1]+direct[1]*length)
                if c is not None and self.arena.bottom[c] < p[2]+stats["height"] and self.arena.top[c]-p[2] > max_climb:
                    shortcut = False
                    self.shortcut_until = -1
                    break
        if elevated or shortcut:
            route = direct
        if goal_dist < 9:
            if self.arena.travel_room(p, direct, goal_dist, me["stats"]) >= goal_dist:
                route = direct
        endpoint = goal_dist < (3 if self.mode in ("cube", "laser", "lightning") else 5)
        endpoint = endpoint and goal_dist < max(0.03, zone["next_radius"]*0.1)
        live = [(m,d) for m,d,clear in threats if clear]
        evade = (0, 0)
        if live:
            m, d = live[0]
            rx, ry = direction(p[0]-m["pos"][0], p[1]-m["pos"][1])
            side = 1 if math.sin(t*1.35+self.seed_angle) > 0 else -1
            retreat = 0.65 if d < 40 or pressured else 0.15
            secondary = me["weapons"][1]
            hp_fraction = me["hp"]/me["max_hp"]
            if not self.avoiding and secondary and secondary["kind"] == "lightning" and len(live) <= 2 and not urgent and hp_fraction > .63 and d < 135:
                if d > secondary["stats"]["range"]*.76:
                    retreat = -3.5
                elif d < 19:
                    retreat = 1.4
                else:
                    retreat = 0
            elif not self.avoiding and len(live) == 1 and not urgent and hp_fraction > 0.62 and not pressured:
                if secondary and secondary["kind"] == "lightning" and 29 < d < 100:
                    retreat = -2.8
                elif m.get("hp", me["hp"]) < me["hp"]*0.64 and 25 < d < 100:
                    retreat = -1.2
            inbound = math.atan2(p[1] - m["pos"][1], p[0] - m["pos"][0])
            faced = abs(angle_delta(inbound, m["aim"][0])) < 0.55
            if faced and not urgent and d < 215:
                side = 1 if math.sin(t * 4.8 + self.seed_angle) > 0 else -1
            if not self.avoiding and len(live) == 1 and not urgent and hp_fraction > 0.60 and d > 31 and not secondary:
                retreat = -2.5 if m.get("hp", 1400) < me["hp"] * 1.15 else -0.5
            evade = direction(rx*retreat-side*ry, ry*retreat+side*rx)
        if endpoint and not live and not obs["projectiles"]:
            return (0, 0), False, False
        # A small set of commands keeps obstacle and zone checks bounded.
        weight = 0 if not live else (0.28 if urgent else 1.6)
        if live and not urgent and not pressured and self.mode in ("laser", "lightning", "cube", "crate", "drop"):
            # A distant or solitary visible actor need not divert us from an upgrade.
            if live[0][1] > 45 and me["hp"] > 0.7*me["max_hp"]:
                weight = 0.23
        if self.mode in ("laser", "lightning") and me["weapons"][1] is None and not urgent:
            weight = 0.12
        if self.mode == "skirt" and goal_dist > 9 and not urgent:
            weight = 0.38 if not pressured else 0.62
        if self.mode=="duel" and not urgent and not pressured:
            weight = 0.42 if goal_dist > 15 else 0.95
        if self.mode == "exit":
            weight = 0.16
        if self.mode == "cover" and not endpoint:
            weight = 0.45
        if self.mode == "drop" and not urgent and not pressured:
            # Stay in the capture ring. A full repair needs four continuous seconds.
            for d in obs.get("drops", ()):
                if (d["lands_in"] == 0 and math.dist(self.goal, d["pos"][:2]) < 1
                        and goal_dist <= d["radius"]):
                    if goal_dist > d["radius"]*0.45:
                        return direct, False, False
                    return (0, 0), False, False
        if zone["radius"] <= 5 and zone["next_radius"] == 0:
            weight = 0
        path_scale = 0.0 if endpoint else 1.0
        dx, dy = direction(path_scale*route[0]+weight*evade[0], path_scale*route[1]+weight*evade[1])
        dangers = self._incoming_projectiles(obs) if not urgent and self.mode not in ("drop", "heal") else []
        if dx == dy == 0:
            if not dangers:
                return (0, 0), False, False
            dx, dy = direction(-dangers[0][4], dangers[0][3])
        base = math.atan2(dy, dx)
        best, best_score = (0, 0), -math.inf
        radius = zone["radius"]
        gap = math.dist(p[:2], zone["center"])-radius
        horizon = min(8, max(2, math.dist(p[:2], waypoint))) if not live else 10
        if shortcut or elevated:
            horizon = min(8, max(2, goal_dist))
        incoming_charge = None
        if not urgent:
            caster = next((m for m, distance in live if m.get("charging") and distance < 250), None)
            if caster:
                ex, ey, ez = caster["pos"]
                yaw = caster["aim"][0]
                normal = (-math.sin(yaw), math.cos(yaw))
                offset = (p[0] - ex) * normal[0] + (p[1] - ey) * normal[1]
                if abs(offset) < 26:
                    elapsed = t - self.charge_since[caster["id"]]
                    remaining = max(0.13, 0.76 - elapsed)
                    incoming_charge = (ex, ey, ez, normal, offset, remaining)
        for angle in (0, 0.48, -0.48, 0.9, -0.9, 1.55, -1.55, math.pi):
            ax, ay = math.cos(base+angle), math.sin(base+angle)
            room = self.arena.travel_room(p, (ax, ay), horizon, me["stats"])
            score = math.cos(angle)*4 + min(horizon, room)*0.65
            if dangers and room > 2:
                score = self._dodge_score(score, me, ax, ay, dangers)
            if incoming_charge and room > 2:
                ex, ey, ez, normal, offset, remaining = incoming_charge
                length = min(room, me["stats"]["walk_speed"] * remaining)
                px, py = p[0] + ax * length, p[1] + ay * length
                separation = abs(offset + length * (ax * normal[0] + ay * normal[1]))
                score += 9 * min(1, separation / (me["stats"]["radius"] + 2))
                if not self.arena.line_open((ex, ey, ez + 7), (px, py, p[2] + 4.5)):
                    score += 9 * 1.2
            if room < 2:
                score -= 15
            next_gap = math.hypot(p[0]+ax*8-zone["center"][0], p[1]+ay*8-zone["center"][1])-radius
            # A route around a wall can briefly leave the disk. Once outside,
            # forcing every individual step inward can pin us against the wall.
            zone_cost = 0.22 if urgent and not live else (1.15 if urgent else 1.8)
            score -= max(0, next_gap-max(0, gap))*zone_cost
            if score > best_score:
                best, best_score = (ax, ay), score
        # In the last few meters, use proportional movement to avoid running through the circle.
        if zone["next_radius"] == 0 and goal_dist < 9 and math.dist(self.goal, zone["next_center"]) < 1:
            d = math.dist(zone["next_center"], p[:2])
            if d < 0.03:
                return (0, 0), False, False
            speed = min(1, d/2.5)
            best = (best[0]*speed, best[1]*speed)
        if t >= self.jet_until and (shortcut or urgent or self.blocked_count):
            # A disconnected ground region may still be reachable using the mech's
            # jet. Inspect only the next few game cells before spending energy.
            for length in (4, 8, 12):
                c = self.arena.cell(p[0]+direct[0]*length, p[1]+direct[1]*length)
                if c is None:
                    continue
                floor = self.arena.floor[c]
                top = self.arena.top[c] if self.arena.bottom[c] < p[2]+me["stats"]["height"] else floor
                rise = max(top, floor) - p[2]
                if rise > me["stats"]["step_up"]:
                    climb_time = rise/me["stats"]["max_rise_speed"] + 0.7
                    if climb_time*me["stats"]["jet_drain"] + 6 < me["energy"] and goal_dist > 3:
                        self.jet_height = p[2]+rise+1.8
                        self.jet_until = t+climb_time+0.7
                    elif rise > me["stats"]["max_rise_speed"]*me["max_energy"]/me["stats"]["jet_drain"]:
                        self.shortcut_until = -1
                    break
        if t < self.jet_until:
            best = direct
            if p[2] < self.jet_height and me["energy"] > 3:
                return best, not me["grounded"], me["grounded"]
        reserve = 65 if shortcut else 45
        self.sprinting = me["energy"] > (reserve if self.sprinting else 90) and urgent
        boosting = self.sprinting and me["grounded"] and goal_dist > 12
        if live and not urgent and me["energy"] > 62:
            boosting = (pressured or live[0][1] < 55) and math.sin(t*2.1) > 0.2
        if self.mode in ("laser", "lightning") and me["weapons"][1] is None and goal_dist > 7:
            boosting = bool(me["grounded"] and me["energy"] > 19 and not self.blocked_count)
        if self.mode == "exit" and not self.blocked_count:
            boosting = me["grounded"] and me["energy"] > 18
        if incoming_charge and not self.blocked_count and me["energy"] > 34 and me["grounded"]:
            boosting = True
        jumping = False
        if t < self.unstick_until and me["grounded"] and me["energy"] >= me["max_energy"]*0.9:
            jumping = True
            self.rescue_until = t+3.6
        if t < self.rescue_until and not me["grounded"]:
            boosting = me["energy"] > 10
        if self.blocked_count >= 2 and me["grounded"] and not jumping:
            boosting = False
        if dangers and not urgent:
            stats = me["stats"]
            use_boost = True and me["energy"] > 25 and me["grounded"]
            speed = stats["boost_speed"] if use_boost else stats["walk_speed"]

            def clear_of(ax, ay, sp):
                ox, oy = ax*sp - me["vel"][0], ay*sp - me["vel"][1]
                change = math.hypot(ox, oy) / stats["ground_accel" if me["grounded"] else "air_accel"]
                worst = 99
                for eta, mx, my, vx, vy in dangers:
                    drift = eta - 0.5*change if eta >= change else 0.5*eta**2/max(0.001, change)
                    worst = min(worst, abs((mx + ox*drift)*vy - (my + oy*drift)*vx)/max(1, math.hypot(vx, vy)))
                return worst
            need = stats["radius"] + 0.75
            if clear_of(best[0], best[1], stats["walk_speed"]) < need:
                options = [(math.cos(k*math.tau/16), math.sin(k*math.tau/16)) for k in range(16)]
                options = [d for d in options if self.arena.travel_room(p, d, 4, stats) > 2]
                if options:
                    pick = max(options, key=lambda d: clear_of(d[0], d[1], speed))
                    if clear_of(pick[0], pick[1], speed) > clear_of(best[0], best[1], stats["walk_speed"]):
                        best, boosting = pick, use_boost
        return best, boosting, jumping

    def act(self, obs):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        p = me["pos"]
        self.mech_kills += sum(e["type"] == "kill" and 0 <= e["target"] < 16 for e in obs["events"])
        self.uncredited_eliminations = max(0, 16 - obs["alive"] - self.mech_kills)
        self.surv = self.uncredited_eliminations >= 1 and obs["alive"] > 3
        self.charge_since = {
            m["id"]: self.charge_since.get(m["id"], t) for m in obs["mechs"] if m.get("charging")
        }
        if t < 45 and t - self.last_search_sample >= 1.5 and me["weapons"][1] is None:
            self.last_search_sample = t
            self.searched.add((int(p[0] // 25), int(p[1] // 25)))
        self._resources(obs)
        threats, npcs = self._visible_contacts(obs)
        live = [m for m, _, clear in threats if clear]
        hits = [e for e in obs["events"] if e["type"] == "damaged" and e.get("bearing") is not None]
        attackers = [e for e in hits if e["weapon"] != "npc"]
        for e in hits:
            if e["weapon"] == "npc":
                self.last_drone = (e.get("by"), t)
            else:
                self.damage_window.append((t, e["amount"]))
        self.damage_window = [(when, amount) for when, amount in self.damage_window if t-when < 2.5]
        if attackers or live:
            self.last_contact = t
            if attackers:
                self.last_bearing = attackers[-1]["bearing"]
            else:
                self.last_bearing = math.atan2(live[0]["pos"][1]-p[1], live[0]["pos"][0]-p[0])
        hp_fraction = me["hp"]/me["max_hp"]
        burst = sum(amount for _, amount in self.damage_window) / me["max_hp"]
        pressured = len(live) >= 2 or hp_fraction < 0.57 or (hp_fraction < 0.88 and burst > 0.19)
        mechs = [m for m, _, _ in threats]
        self.avoiding = self.surv and any(not self._prey(m, mechs) for m in live)
        if self.avoiding:
            pressured = True
        if me["in_zone"] and hp_fraction < 0.88 and (attackers or (pressured and live)):
            missing = max(0, 0.93*me["max_hp"] - me["hp"])
            self.recover_until = t + me["stats"]["regen_delay"] + missing/max(1, me["stats"]["regen_per_s"]) + 1
        outside_next = math.dist(p[:2], zone["next_center"]) - zone["next_radius"]
        time_left = zone["shrink_end"]-t
        urgent = (not me["in_zone"] or (outside_next > 0 and
                  outside_next*1.45/me["stats"]["walk_speed"] + 6 >= time_left))
        state = (zone["next_radius"], pressured, urgent, bool(live))
        separate_fight = self._can_recover_in_view(obs, threats) if live else False
        loot_near = self.uncredited_eliminations >= 1 and any(kind == "cube" and math.dist(p, point) < 160 and abs(point[2]-p[2]) < 5
                        for point, kind, seen in self.loot.values())
        healing = (not loot_near and (not live or separate_fight) and not urgent and me["in_zone"] and outside_next < -4
                   and hp_fraction < 0.93 and (t < self.recover_until or separate_fight)
                   and t-self.last_drone[1] > 2)
        # Prefer a safe, ongoing capture to a slower natural repair.
        if healing and any(d.get("holder") == me["id"] for d in obs.get("drops", ())):
            healing = False
        state = (*state, healing)
        if me["weapons"][1] is None:
            ground_items = tuple(
                item["id"] for item in obs["pickups"]
                if item["kind"] in ("laser", "lightning") and abs(item["pos"][2] - p[2]) <= 4
            )
            state = (*state, ground_items)
        if healing:
            self.goal, self.mode = tuple(p[:2]), "heal"
            self.plan_state = state
        elif t >= self.plan_at or getattr(self, "plan_state", None) != state:
            self._choose_goal(obs, threats, pressured, urgent)
            self.plan_state = state
            self.plan_at = t + (0.8 if pressured or urgent else 1.4)
        if len(threats) > 1:
            self.escape_contact = None
            self.exit_point = None
        elif threats:
            self.escape_contact = threats[0][0]
            self.escape_seen = t
        rival = self.escape_contact
        if (self.mech_kills > 0 and rival and t - self.escape_seen < 12 and not urgent and me["hp"] < 0.84*me["max_hp"]
                and self.mode in ("cover", "heal", "exit") and not (live and rival["hp"] < me["hp"]*0.6)):
            if t >= self.next_exit or self.exit_point is None or math.dist(self.exit_point, p[:2]) < 8:
                self.next_exit = t + 1.4
                away = math.atan2(p[1]-rival["pos"][1], p[0]-rival["pos"][0])
                eye = (*rival["pos"][:2], rival["pos"][2]+self._actor_center(rival)*1.55)
                best_exit = None
                for turn in (0, -0.6, 0.6, -1.15, 1.15, -1.57, 1.57):
                    for length in (22, 45, 72):
                        xy = p[0]+length*math.cos(away+turn), p[1]+length*math.sin(away+turn)
                        c = self.arena.cell(*xy)
                        if c is None or self.arena.clearance[c] < me["stats"]["radius"]+2:
                            continue
                        if math.dist(xy,zone["center"]) > zone["radius"]-8:
                            continue
                        covered = not self.arena.line_open(eye, (*xy,self.arena.floor[c]+me["stats"]["muzzle_height"]))
                        gap = math.dist(xy, rival["pos"][:2]) - math.dist(p[:2],rival["pos"][:2])
                        cost = 0.3*length - 0.7*gap - 40*covered
                        cost += 0.6*max(0, math.dist(xy,zone["next_center"])-zone["next_radius"])
                        if best_exit is None or cost < best_exit[0]:
                            best_exit = cost,xy
                self.exit_point = best_exit[1] if best_exit else None
            if self.exit_point:
                self.goal, self.mode = self.exit_point, "exit"
                healing = False
                self.plan_at = t
        if self.last_probe is None or t-self.last_probe[0] >= 1.25:
            if self.last_probe is not None and math.hypot(*self.previous_move) > 0.5 and math.dist(p[:2], self.last_probe[1][:2]) < 2.4:
                self.blocked_count += 1
                if self.blocked_count == 2:
                    self.nav.key = None
                self.unstick_until = t + min(3.6, 0.8+self.blocked_count*0.7)
            else:
                self.blocked_count = 0
            self.last_probe = (t, tuple(p))
        if healing:
            move, boost, jump = (0, 0), False, False
        else:
            move, boost, jump = self._move(obs, threats, urgent, pressured)
        self.previous_move = move
        act = {"tick": obs["tick"], "move": move, "boost": boost, "jump": jump}
        self._loadout(obs, act)
        target, center = self._target(obs, threats, npcs, urgent)
        if t-self.last_contact < 2:
            yaw = self.last_bearing
        else:
            sounds = [s for s in obs["sounds"] if s["kind"] in ("gun", "laser", "lightning", "mortar", "laser_charge")]
            yaw = sounds[-1]["bearing"] if sounds else self.seed_angle + t*0.95
        if target is None and ((me["weapons"][1] is None and t < 70) or getattr(self, "farming", False)) and t - self.last_contact > 1:
            # Complete a sight sweep during the opening without changing leg direction.
            yaw = me["aim"][0] + 0.37
        act["aim"] = [angle_delta(yaw, 0), 0]
        self._weapons(obs, target, center, act, urgent)
        return act


def run():
    pending = b""
    bot = None
    last_tick = -1
    while True:
        chunk = os.read(0, 65536)
        if not chunk:
            return
        pending += chunk
        while select.select([0], [], [], 0)[0]:
            chunk = os.read(0, 65536)
            if not chunk:
                break
            pending += chunk
        lines = pending.split(b"\n")
        pending = lines.pop()
        latest = None
        for line in lines:
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg["type"] == "end":
                return
            if msg["type"] == "hello":
                bot = Bot(msg)
                last_tick = -1
                latest = None
                print('{"type":"ready","name":"Eagle Eye"}', flush=True)
            elif msg["type"] == "obs" and msg["tick"] > last_tick:
                if latest is None or msg["tick"] > latest["tick"]:
                    latest = msg
        if latest is not None:
            last_tick = latest["tick"]
            print(json.dumps(bot.act(latest), allow_nan=False, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    run()
