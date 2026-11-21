"""House-squad proxy: four levels of pressure; roles use only game observations."""

import math
from .game import angle, unit


# Fixed 4/4/4/3 tier order. Only the calibration split may select the mix.
def roster(mix, strong=False):
    if strong:
        # Frozen historical policies. Each controller sees only its own observation.
        return ["phase3"] * 6 + ["equipment"] * 4 + ["circle"] * 2 + ["phase2"] * 3
    easy = ["starter"] * (4 - mix) + ["hunter"] * mix
    return easy + [
        "looter",
        "starter",
        "edge",
        "hunter",
        "camper",
        "looter",
        "edge",
        "hunter",
        "phase1",
        "phase2",
        "hunter",
    ]


# Real house fillers captured supply drops in the public exhibitions (p2-house,
# cast-late, cast-live). Sound reaction is an assumption kept switchable.
HOUSE_DROPS = True
HOUSE_SOUND = True
# Whether a squad member that is not yet hunting shoots back when the subject
# hits it or comes close. The track page does not say; keep it switchable.
HOUSE_DEFEND = False
HEARD_KINDS = ("gun", "laser", "laser_charge", "lightning", "mortar", "explosion", "boost", "reload")


def hear(g, p):
    """Remember a noisy estimate of the latest audible rival sound."""
    pos = p.s["pos"]
    ranges = g.cfg["sound"]["range"]
    for kind, by, point, when in g.sounds:
        if by == p.id or by >= 16 or g.t - when > 0.101 or kind not in HEARD_KINDS:
            continue
        if not g.ffa and by != g.subject:
            continue
        d = math.dist(point, pos)
        if d > ranges.get(kind, 0):
            continue
        bearing = math.atan2(point[1] - pos[1], point[0] - pos[0]) + p.rng.gauss(0, 0.12)
        d *= 0.8 + 0.4 * p.rng.random()
        p.heard = ((pos[0] + d * math.cos(bearing), pos[1] + d * math.sin(bearing)), g.t)


def scripted(g, p):
    s = p.s
    pos = s["pos"]
    role = p.role
    active = p.id in g.active or g.ffa
    if g.ffa:
        peers = [q for q in g.players if q.life and q is not p and g.visible(p, q.s["pos"], 9)]
        enemy = min(peers, key=lambda q: math.dist(pos, q.s["pos"]), default=None)
    else:
        enemy = g.players[g.subject] if active and g.t - g.team_seen < 5 else None
        if HOUSE_DEFEND and enemy is None and g.players[g.subject].life:
            hit = getattr(p, "last_hit_by", None)
            subject = g.players[g.subject]
            if (hit and hit[0] == g.subject and g.t - hit[1] < 6) or (
                    math.dist(pos, subject.s["pos"]) < 60 and g.visible(p, subject.s["pos"], 9)):
                enemy = subject
    zone = g.zone
    dest = zone["next_center"]
    gun = s["weapons"][0]
    side = s["weapons"][1]
    a = {"fire": [False, False], "reload": False}
    if p.offer:
        a["pick"] = 0
    target = None
    if enemy is not None:
        pt = enemy.s["pos"]
        delta = math.dist(pos, pt)
        if g.geo.clear((*pos[:2], pos[2] + 7), (*pt[:2], pt[2] + 4.5)):
            target = pt
        if role == "hunter" or delta > 120:
            dest = pt
    elif active and not g.ffa and g.shared_pos is not None and g.t - g.team_seen < 16:
        dest = g.shared_pos
    elif HOUSE_SOUND and active and getattr(p, "heard", None) and g.t - p.heard[1] < 8:
        dest = p.heard[0]
    if HOUSE_SOUND and active and enemy is None:
        hear(g, p)
    # Scripted policies check nearby loot periodically and keep their waypoint.
    if g.tick % 12 < 2 or not hasattr(p, "resource"):
        best = 65 if role == "looter" else 30
        p.resource = None
        for it in list(g.cubes.values()) + ([] if side else list(g.pickups.values())):
            q = it["pos"]
            d = math.dist(pos, q)
            if d < best and abs(q[2] - pos[2]) < 8 and g.visible(p, q):
                best = d
                p.resource = (it["id"], q)
        if role == "looter" and p.resource is None:
            for it in g.crates.values():
                d = math.dist(pos, it["pos"])
                if d < best and g.visible(p, it["pos"]):
                    best = d
                    p.resource = (it["id"], it["pos"])
    if p.resource and enemy is None:
        item, pt = p.resource
        dest = pt
        if item in g.pickups and math.dist(pos, pt) < 4:
            a["take"] = item
        if item in g.crates:
            target = pt
    holding = False
    if HOUSE_DROPS and enemy is None:
        for d in g.drops.values():
            far = math.dist(pos[:2], d["pos"][:2])
            if far < 300 and d["lands_in"] <= far / 11.7 + 3 and d["holder"] in (None, p.id):
                dest = d["pos"]
                holding = far < d["radius"] * 0.6
                break
    if role == "edge" and enemy is None and not p.resource and not holding:
        theta = p.id * 2.399
        dest = [
            dest[0] + zone["next_radius"] * 0.54 * math.cos(theta),
            dest[1] + zone["next_radius"] * 0.54 * math.sin(theta),
        ]
    mx, my = (0, 0) if holding else unit(dest[0] - pos[0], dest[1] - pos[1])
    if target is not None:
        dx, dy = target[0] - pos[0], target[1] - pos[1]
        d = math.hypot(dx, dy)
        if enemy and role != "looter":
            lead = d / 252
            dx += enemy.s["vel"][0] * lead
            dy += enemy.s["vel"][1] * lead
        yaw = math.atan2(dy, dx)
        pitch = math.atan2(target[2] + (4.5 if enemy else 1) - pos[2] - 7, d)
        if enemy and d < 110 and s["in_zone"] and role != "camper":
            sign = 1 if math.sin(g.t * 1.2 + p.id) > 0 else -1
            rx, ry = unit(dx, dy)
            mx, my = unit(0.3 * mx - sign * ry, 0.3 * my + sign * rx)
        a["fire"] = [abs(angle(yaw - s["aim"][0])) < 0.22, False]
        if side:
            if side["kind"] == "lightning":
                a["fire"][1] = d < 45
            elif side["kind"] == "laser":
                a["fire"][1] = side["charge"] < side["stats"]["charge"]
            elif side["kind"] == "mortar":
                a["fire"][1] = d > 12
    else:
        yaw = g.t * 0.75 + p.id
        pitch = 0
        if gun:
            a["reload"] = gun.get("ammo", 12) < 8
        if role == "camper" and math.dist(pos[:2], dest[:2]) < zone["next_radius"] * 0.6:
            mx = my = 0
    # Expert fillers fired from inside the zone while boosting in both public
    # exhibitions. Use short combat pulses without assuming their private input.
    combat_pulse = (
        not g.ffa and target is not None and enemy is not None
        and s["energy"] > 28 and (g.tick // 2 + p.id) % 5 in (0, 2)
        and role not in ("looter", "camper")
    )
    a.update(
        move=[mx, my], aim=[yaw, pitch],
        boost=s["grounded"] and (not s["in_zone"] or combat_pulse), jump=False,
    )
    if mx or my:
        arena = g.geo.arena
        cell = arena.cell(pos[0] + mx * 4, pos[1] + my * 4)
        if cell is not None and arena.clearance[cell] < 4 and pos[2] < arena.top[cell]:
            if s["energy"] > 25:
                a["jump"] = s["grounded"]
                a["boost"] = not s["grounded"]
    return a

# Frozen independent qualifying-field proxy. Roles are randomly assigned to spawn slots.
QUALIFIERS = (
    "starter", "phase1", "phase2", "phase3", "phase4", "phase5",
    "hunter", "hunter", "hunter", "equipment", "circle",
    "laser_scout", "cube_farmer", "cube_farmer", "ring_camper",
)


def qualifying_controller(role, incumbent):
    """Exhibition-inspired styles. Every decision uses the actor's own observation."""
    class Competitor(incumbent):
        def _choose_goal(self, obs, threats, pressured, urgent):
            super()._choose_goal(obs, threats, pressured, urgent)
            if urgent:
                return
            me, zone = obs["self"], obs["zone"]
            pos = me["pos"]
            if role == "ring_camper":
                if math.dist(pos[:2], zone["next_center"]) < max(0, zone["next_radius"] - 15):
                    self.goal, self.mode = tuple(pos[:2]), "hold"
                return
            if role == "laser_scout" and me["weapons"][1]:
                return
            items = obs["pickups"] if role == "laser_scout" else obs["cubes"] or obs["crates"]
            viable = [i for i in items if (role != "laser_scout" or i["kind"] == "laser")
                      and abs(i["pos"][2]-pos[2]) < 5
                      and math.dist(i["pos"][:2], zone["center"]) < zone["radius"]-5
                      and math.dist(i["pos"], pos) < (180 if role == "laser_scout" else 75)]
            if viable:
                item = min(viable, key=lambda i: math.dist(pos, i["pos"]))
                self.goal = tuple(item["pos"][:2])
                self.mode = "laser" if role == "laser_scout" else "cube"
    return Competitor
