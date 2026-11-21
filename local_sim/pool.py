"""House-squad proxy: four levels of pressure; roles use only game observations."""

import math
from .game import angle, unit


# Fixed 4/4/4/3 tier order. Only the calibration split may select the mix.
def roster(mix):
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
    if role == "edge" and enemy is None and not p.resource:
        theta = p.id * 2.399
        dest = [
            dest[0] + zone["next_radius"] * 0.54 * math.cos(theta),
            dest[1] + zone["next_radius"] * 0.54 * math.sin(theta),
        ]
    mx, my = unit(dest[0] - pos[0], dest[1] - pos[1])
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
    a.update(move=[mx, my], aim=[yaw, pitch], boost=not s["in_zone"] and s["grounded"], jump=False)
    if mx or my:
        arena = g.geo.arena
        cell = arena.cell(pos[0] + mx * 4, pos[1] + my * 4)
        if cell is not None and arena.clearance[cell] < 4 and pos[2] < arena.top[cell]:
            if s["energy"] > 25:
                a["jump"] = s["grounded"]
                a["boost"] = not s["grounded"]
    return a
