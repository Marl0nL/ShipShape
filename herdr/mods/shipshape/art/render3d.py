#!/usr/bin/env python3
"""Renders the chart's sprites as lit 3D models (numpy only): ships as signed
distance fields, islands as heightfields, seen from one oblique camera under a
low warm sun. Writes PNGs into ../img and their anchor points into ../sprites.js.

    python3 mods/shipshape/art/render3d.py            # everything
    python3 mods/shipshape/art/render3d.py frigate-3-r  # one sprite, for a look
"""

import json
import os as _os

_os.environ.setdefault("OMP_NUM_THREADS", "1")
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import math
import os
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "..", "img")
F = np.float32

EL = math.radians(36)  # the camera looks down at this angle, from the south
SIN, COS = math.sin(EL), math.cos(EL)
SUN_W = np.array([-0.60, 0.50, 0.62])
SUN_W /= np.linalg.norm(SUN_W)
SUN_C = np.array([1.30, 1.02, 0.74], F)  # a low sun
SKY_C = np.array([0.34, 0.44, 0.60], F)
PX = 60  # final px per world unit, ships
SS = 2  # supersampling


def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def cap(P, a, b, r):
    a, b = np.asarray(a, F), np.asarray(b, F)
    pa, ba = P - a, b - a
    h = np.clip((pa @ ba) / (ba @ ba), 0, 1)
    return np.linalg.norm(pa - h[:, None] * ba, axis=1) - r


def sphere(P, c, r):
    return np.linalg.norm(P - np.asarray(c, F), axis=1) - r


def box(P, c, half):
    q = np.abs(P - np.asarray(c, F)) - np.asarray(half, F)
    return np.linalg.norm(np.maximum(q, 0), axis=1) + np.minimum(q.max(axis=1), 0)


def tri2d(px, py, a, b, c):
    """Signed distance to a triangle in 2D."""

    def edge(p0, p1):
        ex, ey = p1[0] - p0[0], p1[1] - p0[1]
        wx, wy = px - p0[0], py - p0[1]
        h = np.clip((wx * ex + wy * ey) / (ex * ex + ey * ey), 0, 1)
        dx, dy = wx - ex * h, wy - ey * h
        return dx * dx + dy * dy, ex * wy - ey * wx

    d0, s0 = edge(a, b)
    d1, s1 = edge(b, c)
    d2, s2 = edge(c, a)
    sign = np.sign((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]))
    inside = (sign * s0 > 0) & (sign * s1 > 0) & (sign * s2 > 0)
    d = np.sqrt(np.minimum(np.minimum(d0, d1), d2))
    return np.where(inside, -d, d)


# ----------------------------------------------------------------- ships

SHIPS = {
    "dinghy": dict(L=0.46, B=0.2, draft=0.10, free=0.11, castle=0.0, fore=0.0, masts=[], ports=0),
    "sloop": dict(
        L=1.0,
        B=0.37,
        draft=0.2,
        free=0.25,
        castle=0.05,
        fore=0.0,
        masts=[(0.08, 2.05, 0)],
        ports=0,
        rig="sloop",
    ),
    "brig": dict(
        L=1.35,
        B=0.45,
        draft=0.24,
        free=0.31,
        castle=0.11,
        fore=0.03,
        masts=[(-0.38, 2.1, 0.52), (0.5, 1.95, 0.47)],
        tiers=2,
        main=0,
        ports=5,
    ),
    "frigate": dict(
        L=1.7,
        B=0.52,
        draft=0.26,
        free=0.35,
        castle=0.14,
        fore=0.04,
        masts=[(-0.8, 1.9, 0.46), (-0.08, 2.3, 0.56), (0.72, 2.0, 0.48)],
        tiers=2,
        main=1,
        ports=8,
    ),
    "galleon": dict(
        L=2.0,
        B=0.64,
        draft=0.3,
        free=0.42,
        castle=0.30,
        fore=0.1,
        masts=[(-1.0, 2.15, 0.48), (-0.12, 2.8, 0.64), (0.88, 2.3, 0.52)],
        tiers=3,
        main=1,
        ports=9,
    ),
}
SAILS = {"dinghy": 0, "sloop": 2, "brig": 4, "frigate": 6, "galleon": 9}
HULL, MAST, SAIL, FURL, LAMP, ROPE, CABIN = range(7)
BRACE = math.radians(14)  # yards are braced round toward the camera


def hull_parts(P, c):
    x, y, _z = P[:, 0], P[:, 1], P[:, 2]
    L, B = c["L"], c["B"]
    t = np.clip(x / L, -1, 1)
    plan = np.where(
        t > 0, np.maximum(1 - np.abs(t) ** 2.3, 0) ** 0.62, 0.78 + 0.22 * (1 - np.abs(t) ** 3)
    )
    k = np.clip((y + c["draft"]) / (c["free"] + c["draft"]), 0, 1.3)
    w = B * plan * (0.5 + 0.5 * np.minimum(k, 1) ** 0.55)
    deck = (
        c["free"]
        + 0.07 * L * np.maximum(t, 0) ** 2
        + c["castle"] * smooth(-0.5, -0.56, t)
        + c["fore"] * smooth(0.62, 0.68, t)
    )
    return w, deck


def hull(P, c):
    w, deck = hull_parts(P, c)
    az = np.abs(P[:, 2])
    rail = 0.075 * smooth(w - 0.085, w - 0.045, az)
    d = np.maximum.reduce(
        [az - w, P[:, 1] - (deck + rail), -c["draft"] - P[:, 1], np.abs(P[:, 0]) - c["L"]]
    )
    return d * 0.6


def build(name, n):
    """The ship's parts in its own frame (x forward, y up, z to starboard):
    [(sdf, material, info)]."""
    c = SHIPS[name]
    L, free = c["L"], c["free"]
    parts = [(lambda P: hull(P, c), HULL, None)]
    stern_y = free + c["castle"]
    lamp = (-L - 0.03, stern_y + 0.24, 0)
    parts.append(
        (lambda P: cap(P, (-L + 0.02, stern_y, 0), (lamp[0], lamp[1] - 0.05, 0), 0.014), MAST, None)
    )
    parts.append((lambda P: sphere(P, lamp, 0.05), LAMP, None))
    deckhouse = {
        "sloop": [(-0.52, 0.2, 0.075, 0.16), (0.42, 0.11, 0.022, 0.1)],
        "brig": [(-0.9, 0.2, 0.08, 0.2), (0.05, 0.16, 0.025, 0.13), (0.9, 0.1, 0.022, 0.1)],
        "frigate": [
            (-1.25, 0.2, 0.08, 0.22),
            (-0.45, 0.14, 0.025, 0.14),
            (0.32, 0.14, 0.025, 0.14),
            (1.15, 0.1, 0.022, 0.1),
        ],
        "galleon": [(-0.55, 0.16, 0.03, 0.17), (0.4, 0.16, 0.03, 0.17), (1.35, 0.14, 0.05, 0.14)],
    }.get(name, [])
    for hx, hl, hh, hw in deckhouse:

        def house(P, hx=hx, hl=hl, hh=hh, hw=hw):
            _, dk = hull_parts(np.array([[hx, 0, 0]], F), c)
            return box(P, (hx, float(dk[0]) + hh, 0), (hl, hh, hw)) - 0.008

        parts.append((house, CABIN, None))
    if not c["masts"]:
        # Oars shipped across the thwarts.
        parts.append(
            (
                lambda P: cap(P, (-0.2, free + 0.03, -0.1), (0.3, free + 0.06, -0.22), 0.014),
                MAST,
                None,
            )
        )
        parts.append(
            (
                lambda P: cap(P, (-0.2, free + 0.03, 0.1), (0.3, free + 0.06, 0.22), 0.014),
                MAST,
                None,
            )
        )
        return parts, c, lamp, None
    bow_y = free + 0.07 * L + c["fore"]
    tip = (L + 0.48, bow_y + 0.3, 0)
    parts.append((lambda P: cap(P, (L * 0.82, bow_y + 0.02, 0), tip, 0.032), MAST, None))
    u = np.array([-math.sin(BRACE), 0, math.cos(BRACE)], F)  # along a yard
    nrm = np.array([math.cos(BRACE), 0, math.sin(BRACE)], F)  # a square sail's face
    tops = []
    for mx, mh, _ in c["masts"]:
        top = (mx, free + mh, 0)
        tops.append(top)
        parts.append(
            (lambda P, mx=mx, top=top: cap(P, (mx, free - 0.05, 0), top, 0.036), MAST, None)
        )
    main = tops[c.get("main", 0)]
    # Stays: bowsprit to the foremast, mast to mast, mainmast to the stern.
    line = [tip] + [tops[i] for i in reversed(range(len(tops)))] + [(-L, stern_y + 0.05, 0)]
    for a, b in zip(line, line[1:], strict=False):
        parts.append((lambda P, a=a, b=b: cap(P, a, b, 0.009), ROPE, None))
    for mx, mh, _ in c["masts"]:
        for side in (-1, 1):
            parts.append(
                (
                    lambda P, mx=mx, mh=mh, side=side: cap(
                        P,
                        (mx, free + mh * 0.82, 0),
                        (mx - 0.12, free + 0.04, side * c["B"] * 0.86),
                        0.008,
                    ),
                    ROPE,
                    None,
                )
            )

    if c.get("rig") == "sloop":
        mx, mh, _ = c["masts"][0]
        g = math.radians(16)
        aft = np.array([-math.cos(g), 0, -math.sin(g)], F)  # the boom, swung out to leeward
        side = np.array([-math.sin(g), 0, math.cos(g)], F)
        boom_y, boom_l = free + 0.3, 1.02
        base = np.array([mx, 0, 0], F)
        parts.append(
            (
                lambda P: cap(
                    P,
                    (mx, boom_y, 0),
                    tuple(base + aft * boom_l + np.array([0, boom_y + 0.03, 0], F)),
                    0.026,
                ),
                MAST,
                None,
            )
        )
        if n >= 1:

            def mainsail(P):
                q = P - base
                a, f = q @ aft, q @ side
                tri = tri2d(
                    a,
                    P[:, 1],
                    (0.03, boom_y + 0.05),
                    (boom_l - 0.04, boom_y + 0.07),
                    (0.03, free + mh - 0.1),
                )
                belly = (
                    0.13
                    * np.clip(1 - np.abs(a - 0.4) / 0.6, 0, 1)
                    * np.clip((free + mh - P[:, 1]) / mh, 0, 1)
                )
                return np.maximum(tri, np.abs(f - belly) - 0.012) * 0.8

            parts.append((mainsail, SAIL, ("fa", base, aft)))
        else:
            parts.append(
                (
                    lambda P: cap(
                        P,
                        (mx - 0.05, boom_y + 0.06, 0),
                        tuple(base + aft * (boom_l - 0.1) + np.array([0, boom_y + 0.09, 0], F)),
                        0.055,
                    ),
                    FURL,
                    None,
                )
            )
        if n >= 2:
            fwd = np.array([math.cos(g * 0.7), 0, math.sin(g * 0.7)], F)
            side2 = np.array([-math.sin(g * 0.7), 0, math.cos(g * 0.7)], F)
            reach = tip[0] - mx - 0.06

            def jib(P):
                q = P - base
                a, f = q @ fwd, q @ side2
                tri = tri2d(
                    a,
                    P[:, 1],
                    (0.07, free + mh * 0.86),
                    (reach, tip[1] + 0.04),
                    (0.12, free + 0.42),
                )
                belly = 0.1 * np.clip(1 - np.abs(a - reach * 0.4) / (reach * 0.6), 0, 1)
                return np.maximum(tri, np.abs(f - belly) - 0.012) * 0.8

            parts.append((jib, SAIL, ("fa", base, fwd)))
        return parts, c, lamp, main

    tiers = c["tiers"]
    order = sorted(range(len(c["masts"])), key=lambda m: (m != c["main"], -m))
    slots = [(m, t) for t in range(tiers) for m in order]
    set_ = set(slots[:n])
    for m, (mx, mh, hw0) in enumerate(c["masts"]):
        th = (mh - 0.3) / tiers
        for t in range(tiers):
            hw = hw0 * (1 - 0.2 * t)
            y0 = free + 0.26 + th * t
            y1 = y0 + th - 0.08
            centre = np.array([mx, 0, 0], F) + nrm * 0.07
            ya = tuple(centre - u * (hw + 0.05) + np.array([0, y1, 0], F))
            yb = tuple(centre + u * (hw + 0.05) + np.array([0, y1, 0], F))
            parts.append((lambda P, ya=ya, yb=yb: cap(P, ya, yb, 0.024), MAST, None))
            if (m, t) in set_:

                def sail(P, centre=centre, hw=hw, y0=y0, y1=y1, t=t):
                    q = P - centre
                    s, f = q @ u, q @ nrm
                    yc, hh = (y0 + y1) / 2, (y1 - y0) / 2
                    v = (P[:, 1] - yc) / hh
                    belly = (
                        (0.16 - 0.03 * t)
                        * np.clip(1 - (s / hw) ** 2, 0, 1)
                        * np.clip(1 - 0.55 * v * v, 0, 1)
                    )
                    width = hw * (1.0 + 0.06 * np.clip(-v, -1, 1))
                    return (
                        np.maximum.reduce(
                            [
                                np.abs(s) - width,
                                np.abs(P[:, 1] - yc) - hh,
                                np.abs(f - belly) - 0.012,
                            ]
                        )
                        * 0.75
                    )

                parts.append((sail, SAIL, ("sq", centre, u)))
            else:
                fa = tuple(centre - u * hw + np.array([0, y1 - 0.045, 0], F))
                fb = tuple(centre + u * hw + np.array([0, y1 - 0.045, 0], F))
                parts.append((lambda P, fa=fa, fb=fb: cap(P, fa, fb, 0.045), FURL, None))
        if m == c["main"] and name != "brig":
            parts.append(
                (
                    lambda P, mx=mx, mh=mh: (
                        box(P, (mx, free + mh * 0.66, 0), (0.085, 0.035, 0.085)) - 0.01
                    ),
                    MAST,
                    None,
                )
            )
    return parts, c, lamp, main


def frame(face):
    """The ship's axes in the world: its bow points across the screen and a little toward the
    camera."""
    a = math.radians(40)
    c, s = math.cos(a), math.sin(a)
    bow = np.array([c if face[1] == "e" else -c, 0, s if face[0] == "s" else -s])
    star = np.array([-bow[2], 0, bow[0]])
    return bow, star


def to_local(v, bow, star):
    return np.array([v @ bow, v[1], v @ star], F)


def soft_shadow(dist, P, Ldir, steps=26, k=9.0, tmax=3.2):
    res = np.ones(len(P), F)
    t = np.full(len(P), 0.03, F)
    for _ in range(steps):
        d = dist(P + Ldir * t[:, None])
        res = np.minimum(res, k * d / t)
        t += np.clip(d, 0.025, 0.25)
    return np.clip(res, 0, 1)


def render_ship(name, n, face):
    parts, c, lamp, main = build(name, n)
    bow, star = frame(face)
    fns = [p[0] for p in parts]
    mats = np.array([p[1] for p in parts])

    def dist(P):
        d = fns[0](P)
        for fn in fns[1:]:
            d = np.minimum(d, fn(P))
        return d

    right, up, fwd = (
        to_local(v, bow, star)
        for v in (np.array([1.0, 0, 0]), np.array([0, COS, -SIN]), np.array([0, -SIN, -COS]))
    )
    sun = to_local(SUN_W, bow, star)
    L, B = c["L"], c["B"]
    top = c["free"] + max([m[1] for m in c["masts"]] + [0.5]) + 0.2
    corners = np.array(
        [[x, y, z] for x in (-L - 1.0, L + 0.75) for y in (0, top) for z in (-B - 0.55, B + 0.55)],
        F,
    )
    cu, cv = corners @ right, corners @ up
    u0, u1, v0, v1 = cu.min() - 0.1, cu.max() + 0.1, cv.min() - 0.1, cv.max() + 0.1
    W, H = int((u1 - u0) * PX) * SS, int((v1 - v0) * PX) * SS
    us = u0 + (np.arange(W) + 0.5) / (PX * SS)
    vs = v1 - (np.arange(H) + 0.5) / (PX * SS)
    U, V = np.meshgrid(us.astype(F), vs.astype(F))
    orig = U.reshape(-1, 1) * right + V.reshape(-1, 1) * up - fwd * 12.0
    N = len(orig)
    sin_l = -fwd[1]
    t_top = (orig[:, 1] - top) / sin_l
    t_sea = orig[:, 1] / sin_l

    # March every ray down to the sea.
    t = t_top.copy()
    hit = np.zeros(N, bool)
    live = np.arange(N)
    for _ in range(110):
        if not len(live):
            break
        d = dist(orig[live] + fwd * t[live, None])
        got = d < 0.0035
        hit[live[got]] = True
        t[live] += np.maximum(d, 0.0035)
        live = live[~got & (t[live] < t_sea[live])]

    rgb = np.zeros((N, 3), F)
    alpha = np.zeros(N, F)
    glow = np.zeros(N, F)

    idx = np.nonzero(hit)[0]
    P = orig[idx] + fwd * t[idx, None]
    D = np.stack([fn(P) for fn in fns])
    which = D.argmin(0)
    mat = mats[which]
    e = 0.004
    k = np.array([[1, -1, -1], [-1, -1, 1], [-1, 1, -1], [1, 1, 1]], F)
    nrm = sum(kk * dist(P + kk * e)[:, None] for kk in k)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True) + 1e-9
    sh = soft_shadow(dist, P + nrm * 0.012, sun)
    ao = np.clip(0.35 + 0.65 * dist(P + nrm * 0.09) / 0.09, 0, 1)
    ndl = nrm @ sun

    x, y, z = P[:, 0], P[:, 1], P[:, 2]
    alb = np.zeros((len(P), 3), F)
    emit = np.zeros(len(P), F)
    # Hull: planked sides, an ochre wale, dark gunports, a pale deck, lit stern windows.
    hm = mat == HULL
    w, deck = hull_parts(P, c)
    planks = 0.82 + 0.18 * smooth(0.12, 0.3, np.abs(((y * 17) % 1.0) - 0.5))
    side = np.array([0.33, 0.17, 0.085], F) * planks[:, None]
    side = np.where((y < 0.08)[:, None], np.array([0.13, 0.075, 0.05], F), side)
    wale = (y > deck - 0.135) & (y < deck - 0.075)
    side = np.where(wale[:, None], np.array([0.86, 0.60, 0.20], F), side)
    if c["ports"]:
        band = (y > c["free"] - 0.25) & (y < c["free"] - 0.07)
        side = np.where(
            band[:, None],
            np.array([0.78, 0.58, 0.26], F) * planks[:, None],
            np.array([0.105, 0.07, 0.05], F) * (0.8 + 0.2 * planks)[:, None],
        )
        side = np.where(wale[:, None], np.array([0.86, 0.60, 0.20], F), side)
        ph = ((x / (2 * L) + 0.5) * c["ports"] * 1.25) % 1.0
        port = (
            (y > c["free"] - 0.2)
            & (y < c["free"] - 0.12)
            & (ph > 0.3)
            & (ph < 0.62)
            & (np.abs(x) < L * 0.78)
            & (np.abs(nrm[:, 1]) < 0.6)
        )
        side = np.where(port[:, None], np.array([0.05, 0.03, 0.02], F), side)
    deck_c = (
        np.array([0.60, 0.44, 0.27], F)
        * (0.86 + 0.14 * smooth(0.1, 0.3, np.abs(((z * 15) % 1.0) - 0.5)))[:, None]
    )
    on_deck = (nrm[:, 1] > 0.75) & (np.abs(z) < w - 0.05)
    hull_c = np.where(on_deck[:, None], deck_c, side)
    stern = (
        (nrm[:, 0] < -0.75)
        & (y > c["free"] - 0.02 + (0.0 if c["castle"] > 0.2 else -0.1))
        & (y < deck - 0.14)
        & (np.abs(z) < w * 0.7)
        & (np.abs(((z * (9 if c["castle"] > 0.2 else 6)) % 1.0) - 0.5) < 0.3)
    )
    alb[hm] = hull_c[hm]
    emit[hm & stern] = 0.4
    cm = mat == CABIN
    roof = nrm[:, 1] > 0.6
    alb[cm] = np.where(
        roof[cm][:, None], np.array([0.22, 0.13, 0.08], F), np.array([0.62, 0.42, 0.24], F)
    )
    lit = (
        cm
        & ~roof
        & (np.abs(((x * 9 + z * 9) % 1.0) - 0.5) < 0.22)
        & (np.abs(((y - c["free"]) * 9) % 1.0 - 0.5) < 0.3)
    )
    emit[lit] = 0.3
    alb[mat == MAST] = (0.34, 0.215, 0.13)
    alb[mat == ROPE] = (0.16, 0.12, 0.09)
    alb[mat == FURL] = (0.80, 0.74, 0.60)
    alb[mat == LAMP] = (1.0, 0.86, 0.55)
    emit[mat == LAMP] = 1.6
    # Canvas: seams, and light coming through from behind.
    sm = mat == SAIL
    if sm.any():
        seam = np.ones(len(P), F)
        for i in np.unique(which[sm]):
            kind, centre, axis = parts[i][2]
            mine = which == i
            s = (P[mine] - centre) @ axis
            seam[mine] = 0.93 + 0.07 * smooth(0.02, 0.07, np.abs(((s * 4.2) % 1.0) - 0.5))
        alb[sm] = np.array([0.93, 0.89, 0.79], F) * seam[sm, None]

    diff = np.clip(ndl, 0, 1) * sh
    through = np.where(sm, np.clip(-ndl, 0, 1) * 0.42 * (0.5 + 0.5 * sh), 0)
    hemi = 0.55 + 0.45 * nrm[:, 1]
    light = (
        SUN_C * (diff + through)[:, None]
        + SKY_C * (hemi * ao)[:, None] * 0.55
        + np.array([0.10, 0.07, 0.04], F) * ao[:, None]
    )
    col = alb * light
    col += emit[:, None] * np.array([1.6, 1.15, 0.55], F)
    # Wet, dark timber right at the waterline.
    col *= (0.55 + 0.45 * smooth(0.0, 0.06, y))[:, None]
    rgb[idx] = col
    alpha[idx] = 1
    glow[idx] = emit

    # The sea around it: the hull's shadow, foam along the waterline, a wake when under way.
    sea = np.nonzero(~hit)[0]
    Pw = orig[sea] + fwd * t_sea[sea, None]
    dh = hull(Pw, c) / 0.6
    shw = soft_shadow(dist, Pw + np.array([0, 0.01, 0], F), sun, steps=30, k=6.0, tmax=5)
    wx, wz = Pw[:, 0], Pw[:, 2]
    rip = 0.5 + 0.5 * np.sin(wx * 31 + np.sin(wz * 23) * 2.2) * np.sin(wz * 37 + wx * 9)
    foam = smooth(0.09, 0.0, dh) * (0.6 + 0.4 * rip)
    if n > 0:
        aft = np.clip(-wx - L * 0.7, 0, None)
        vee = (
            np.exp(-(((np.abs(wz) - 0.16 - aft * 0.26) / (0.05 + aft * 0.05)) ** 2))
            * np.exp(-aft * 1.5)
            * (aft > 0)
            * (0.5 + 0.5 * rip)
        )
        churn = np.exp(-((wz / (0.14 + aft * 0.1)) ** 2)) * np.exp(-aft * 2.0) * (aft > 0) * rip
        bowf = smooth(0.2, 0.0, dh) * smooth(L * 0.35, L * 0.95, wx) * (0.6 + 0.4 * rip)
        foam = np.clip(foam + 1.3 * vee + 0.9 * churn + 1.1 * bowf, 0, 1)
    shade = (1 - shw) * 0.62 * smooth(2.2, 0.3, dh)
    under = smooth(0.0, -0.05, dh) * 0.55  # the hull, seen through the water
    a = 1 - (1 - foam * 0.9) * (1 - shade) * (1 - under)
    dark = np.array([0.01, 0.07, 0.11], F)
    c_sea = (
        dark[None, :] * (1 - foam[:, None])
        + np.array([0.90, 0.97, 1.0], F)[None, :] * foam[:, None]
    )
    rgb[sea] = c_sea
    alpha[sea] = np.clip(a, 0, 1)

    rgb = rgb.reshape(H, W, 3)
    alpha = alpha.reshape(H, W)
    glow = glow.reshape(H, W)
    # Lamplight blooms.
    bloom = (
        ndi.gaussian_filter(np.where(glow > 1.2, glow, glow * 0.25), 9 * SS) * 8.0
        + ndi.gaussian_filter(glow, 2.2 * SS) * 1.2
    )
    warm = np.array([1.0, 0.72, 0.32], F)
    rgb = rgb * alpha[..., None] + warm * np.clip(bloom, 0, 1.2)[..., None] * 0.8
    alpha = np.clip(alpha + np.clip(bloom, 0, 1) * 0.75, 0, 1)
    rgb = np.where(alpha[..., None] > 1e-4, rgb / np.maximum(alpha[..., None], 1e-4), 0)
    out = finish(rgb, alpha, SS)

    def pix(p):
        p = np.asarray(p, F)
        return [round(float((p @ right - u0) * PX), 1), round(float((v1 - p @ up) * PX), 1)]

    meta = dict(w=out.width, h=out.height, at=pix((0, 0, 0)))
    if main is not None:
        meta["mast"] = pix(main)
    out.save(os.path.join(IMG, f"{name}-{n}-{face}.png"), optimize=True)
    return f"{name}-{n}-{face}", meta


def finish(rgb, alpha, ss):
    """Tone, gamma, and down to final size."""
    rgb = np.clip(rgb, 0, None)
    rgb = rgb / (1 + 0.28 * rgb)  # a soft shoulder
    rgb = np.clip(rgb * 1.22, 0, 1) ** (1 / 2.2)
    a = np.clip(alpha, 0, 1)
    arr = np.dstack([rgb * a[..., None], a])
    h, w = arr.shape[0] // ss * ss, arr.shape[1] // ss * ss
    arr = arr[:h, :w].reshape(h // ss, ss, w // ss, ss, 4).mean(axis=(1, 3))
    a = arr[..., 3:4]
    arr[..., :3] = np.where(a > 1e-4, arr[..., :3] / np.maximum(a, 1e-4), 0)
    return Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA")


def ship_jobs():
    return [
        (name, n, face)
        for name in SHIPS
        for n in range(SAILS[name] + 1)
        for face in ("se", "sw", "ne", "nw")
    ]


def run_ship(job):
    return render_ship(*job)


if __name__ == "__main__":
    os.makedirs(IMG, exist_ok=True)
    if len(sys.argv) > 1 and sys.argv[1] not in ("ships", "all"):
        name, n, face = sys.argv[1].split("-")
        print(render_ship(name, int(n), face))
        sys.exit(0)
    with Pool(min(14, os.cpu_count() or 4)) as pool:
        meta = dict(pool.map(run_ship, ship_jobs(), chunksize=1))
    with open(os.path.join(HERE, "ships.json"), "w") as fh:
        json.dump(meta, fh, indent=1)
    print(len(meta), "ship sprites")
