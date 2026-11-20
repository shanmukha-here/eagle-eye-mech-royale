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
        self.loot = {}
        self.plan_at = -1
        self.goal = (0, 0)
        self.mode = ""
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
        for item_id, (point, _, seen) in list(self.loot.items()):
            if t-seen > 14 or (item_id not in visible and math.dist(point, pos) < 7):
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
        if not urgent and not retreat and radius > 7:
            values = {"cube": 44, "lightning": 37, "laser": 36, "crate": 21}
            for point, kind, seen in self.loot.values():
                if kind not in values or (kind in ("lightning", "laser") and all(me["weapons"])):
                    continue
                if abs(point[2]-pos[2]) > 12 or math.dist(pos, point) > 105:
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
        if goals:
            _, gx, gy, self.mode = max(goals)
            self.goal = (gx, gy)
        else:
            self.goal, self.mode = (cx, cy), "zone"

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
                          "gun_rate": 8, "gun_mag": 7.5, "servos": 7, "lightning_range": 7,
                          "laser_damage": 7, "gun_velocity": 6, "jets": 5, "sensors": 5}
            choices = obs["offer"]["choices"]
            if choices:
                act["pick"] = max(range(len(choices)), key=lambda i: priorities.get(choices[i]["id"], 3))
        if any(w is None for w in me["weapons"]):
            p = me["pos"]
            for item in obs["pickups"]:
                s = item["pos"]
                if (item["kind"] in ("laser", "lightning") and abs(s[2]-p[2]) <= 4
                        and math.dist(s[:2], p[:2]) < self.cfg["loot"]["pickup_radius"]):
                    act["take"] = item["id"]
                    break

    def _target(self, obs, threats, npcs, urgent):
        me = obs["self"]
        live = [(m, d) for m, d, clear in threats if clear]
        if self.mode == "heal":
            return None, 0
        if live:
            def priority(pair):
                m, d = pair
                hp = m.get("hp", self.cfg["mech"]["hp"])
                return d*0.5 + min(1, hp/max(1, m.get("max_hp", self.cfg["mech"]["hp"])))*34
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
        for item in sorted(obs["crates"], key=lambda c: math.dist(c["pos"], me["pos"])):
            p, q = me["pos"], item["pos"]
            h = self.cfg["crates"]["height"]*0.5
            if math.dist(p, q) < 64 and self.arena.line_open(
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
            evade = direction(rx*retreat-side*ry, ry*retreat+side*rx)
        if endpoint and not live:
            return (0, 0), False, False
        # A small set of commands keeps obstacle and zone checks bounded.
        weight = 0 if not live else (0.28 if urgent else 1.6)
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
        if dx == dy == 0:
            return (0, 0), False, False
        base = math.atan2(dy, dx)
        best, best_score = (0, 0), -math.inf
        radius = zone["radius"]
        gap = math.dist(p[:2], zone["center"])-radius
        horizon = min(8, max(2, math.dist(p[:2], waypoint))) if not live else 10
        if shortcut or elevated:
            horizon = min(8, max(2, goal_dist))
        for angle in (0, 0.48, -0.48, 0.9, -0.9, 1.55, -1.55, math.pi):
            ax, ay = math.cos(base+angle), math.sin(base+angle)
            room = self.arena.travel_room(p, (ax, ay), horizon, me["stats"])
            score = math.cos(angle)*4 + min(horizon, room)*0.65
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
        jumping = False
        if t < self.unstick_until and me["grounded"] and me["energy"] >= me["max_energy"]*0.9:
            jumping = True
            self.rescue_until = t+3.6
        if t < self.rescue_until and not me["grounded"]:
            boosting = me["energy"] > 10
        if self.blocked_count >= 2 and me["grounded"] and not jumping:
            boosting = False
        return best, boosting, jumping

    def act(self, obs):
        me, zone, t = obs["self"], obs["zone"], obs["t"]
        p = me["pos"]
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
        if me["in_zone"] and hp_fraction < 0.88 and (attackers or (pressured and live)):
            missing = max(0, 0.93*me["max_hp"] - me["hp"])
            self.recover_until = t + me["stats"]["regen_delay"] + missing/max(1, me["stats"]["regen_per_s"]) + 1
        outside_next = math.dist(p[:2], zone["next_center"]) - zone["next_radius"]
        time_left = zone["shrink_end"]-t
        urgent = (not me["in_zone"] or (outside_next > 0 and
                  outside_next*1.45/me["stats"]["walk_speed"] + 6 >= time_left))
        state = (zone["next_radius"], pressured, urgent, bool(live))
        separate_fight = self._can_recover_in_view(obs, threats) if live else False
        healing = ((not live or separate_fight) and not urgent and me["in_zone"] and outside_next < -4
                   and hp_fraction < 0.93 and (t < self.recover_until or separate_fight)
                   and t-self.last_drone[1] > 2)
        # Prefer a safe, ongoing capture to a slower natural repair.
        if healing and any(d.get("holder") == me["id"] for d in obs.get("drops", ())):
            healing = False
        state = (*state, healing)
        if healing:
            self.goal, self.mode = tuple(p[:2]), "heal"
            self.plan_state = state
        elif t >= self.plan_at or getattr(self, "plan_state", None) != state:
            self._choose_goal(obs, threats, pressured, urgent)
            self.plan_state = state
            self.plan_at = t + (0.8 if pressured or urgent else 1.4)
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
