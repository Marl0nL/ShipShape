#!/usr/bin/env python3
"""Renders the chart's world: goal islands and the home harbour as lit
heightfields (same camera and sun as the ships), the tiling sea, its glints,
cloud cover, and the pennants. Writes PNGs into ../img and anchors to world.json.

    python3 mods/shipshape/art/render_world.py [isle-peak|harbour|sea|...]
"""

import os as _os

_os.environ.setdefault("OMP_NUM_THREADS", "1")
_os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
import json
import math
import os
import sys
from multiprocessing import Pool

import numpy as np
from PIL import Image, ImageDraw
from render3d import COS, HERE, IMG, SIN, SKY_C, SUN_C, SUN_W, F, finish, smooth
from scipy import ndimage as ndi

PXI = 150  # final px per world unit, islands
STAGE = os.path.expanduser(
    "~/.local/share/shipshape-art/stage"
)  # what the Blender pass builds from
SS = 2
GROUND, TREE, PINE, PALM, WALL, STONE, TOWER, WOOD, LAVA, LAMP = range(10)


def noise(rng, n, sigma):
    a = ndi.gaussian_filter(rng.standard_normal((n, n)), sigma, mode="wrap")
    return (a / (np.abs(a).max() + 1e-9)).astype(F)


class Land:
    """A heightfield over [-R, R]^2 with a material and a base height per cell."""

    def __init__(self, R, n=640, seed=1):
        self.R, self.n = R, n
        self.rng = np.random.default_rng(seed)
        ax = np.linspace(-R, R, n, dtype=F)
        self.X, self.Z = np.meshgrid(ax, ax)
        self.h = np.zeros((n, n), F)
        self.mat = np.zeros((n, n), np.uint8)
        self.base = np.zeros((n, n), F)
        self.aux = np.zeros((n, n), F)  # wall height, tower height...
        self.lamps = []
        self.towers = []
        self.objs = []  # what stands on the land, for the Blender build

    def shape(self, ax, az, rough=0.2, shelf=0.3):
        """An island's outline: land where the result is > 0."""
        n1 = noise(self.rng, self.n, self.n / 9)
        n2 = noise(self.rng, self.n, self.n / 28)
        r = np.sqrt((self.X / ax) ** 2 + (self.Z / az) ** 2) + rough * n1 + rough * 0.3 * n2
        self.inland = 1 - r  # > 0 on land, growing toward the middle
        return self.inland

    def at(self, x, z):
        i = int(round((z + self.R) / (2 * self.R) * (self.n - 1)))
        j = int(round((x + self.R) / (2 * self.R) * (self.n - 1)))
        return float(self.h[np.clip(i, 0, self.n - 1), np.clip(j, 0, self.n - 1)])

    def gat(self, x, z):
        """The bare ground's height (no trees or walls)."""
        a = getattr(self, "ground", self.h)
        i = int(round((z + self.R) / (2 * self.R) * (self.n - 1)))
        j = int(round((x + self.R) / (2 * self.R) * (self.n - 1)))
        return float(a[np.clip(i, 0, self.n - 1), np.clip(j, 0, self.n - 1)])

    def stamp(self, x, z, r, fn, mat, aux=0.0):
        d = np.sqrt((self.X - x) ** 2 + (self.Z - z) ** 2)
        m = d < r
        g = self.at(x, z)
        new = g + fn(d[m] / r)
        cur = self.h[m]
        take = new > cur
        idx = np.nonzero(m)
        ii, jj = idx[0][take], idx[1][take]
        self.h[ii, jj] = new[take]
        self.mat[ii, jj] = mat
        self.base[ii, jj] = g
        self.aux[ii, jj] = aux

    def tree(self, x, z, s=1.0):
        self.objs.append(dict(kind="tree", x=x, z=z, s=s, g=self.gat(x, z)))
        self.stamp(
            x, z, 0.062 * s, lambda q: 0.02 + 0.105 * s * np.sqrt(np.clip(1 - q * q, 0, 1)), TREE
        )

    def pine(self, x, z, s=1.0):
        self.objs.append(dict(kind="pine", x=x, z=z, s=s, g=self.gat(x, z)))
        self.stamp(x, z, 0.05 * s, lambda q: 0.2 * s * (1 - q) ** 0.9, PINE)

    def palm(self, x, z, s=1.0):
        self.objs.append(dict(kind="palm", x=x, z=z, s=s, g=self.gat(x, z)))
        self.stamp(x, z, 0.012, lambda q: 0.15 * s + 0 * q, WOOD)
        self.stamp(x, z, 0.07 * s, lambda q: (0.15 * s + 0.035 * (1 - q * q)) * (q < 1), PALM)

    def house(self, x, z, w=0.11, d=0.085, wall=0.07, roof=0.055, tint=0):
        self.objs.append(
            dict(
                kind="house", x=x, z=z, w=w, d=d, wall=wall, roof=roof, tint=tint, g=self.gat(x, z)
            )
        )
        g = self.at(x, z)
        m = (np.abs(self.X - x) < w) & (np.abs(self.Z - z) < d)
        ridge = np.clip(1 - np.abs(self.Z - z) / d, 0, 1)
        self.h[m] = g + wall + roof * ridge[m]
        self.mat[m] = WALL
        self.base[m] = g
        self.aux[m] = wall + tint  # the tint rides in the integer part

    def block(self, x, z, w, d, height, mat=STONE):
        self.objs.append(dict(kind="block", x=x, z=z, w=w, d=d, h=height, g=self.gat(x, z)))
        g = self.at(x, z)
        m = (np.abs(self.X - x) < w) & (np.abs(self.Z - z) < d)
        self.h[m] = np.maximum(self.h[m], g + height)
        self.mat[m] = mat
        self.base[m] = g
        self.aux[m] = height

    def tower(self, x, z, r, height, mat=TOWER):
        if mat != WOOD:
            self.objs.append(
                dict(
                    kind="lighthouse" if mat == TOWER else "tower",
                    x=x,
                    z=z,
                    r=r,
                    h=height,
                    g=self.gat(x, z),
                )
            )
        g = self.at(x, z)
        d = np.sqrt((self.X - x) ** 2 + (self.Z - z) ** 2)
        m = d < r
        self.towers.append((x, z, r))
        self.h[m] = g + height
        self.mat[m] = mat
        self.base[m] = g
        self.aux[m] = height

    def jetty(self, x, z, length, lamp=True):
        self.objs.append(dict(kind="jetty", x=x, z=z, length=length))
        m = (np.abs(self.X - x) < 0.045) & (self.Z > z) & (self.Z < z + length)
        self.h[m] = 0.035
        self.mat[m] = WOOD
        self.base[m] = 0
        if lamp:
            self.tower(x, z + length - 0.05, 0.012, 0.16, WOOD)
            self.lamps.append((x, 0.035 + 0.17, z + length - 0.05, 0.9))

    def scatter(self, kind, count, where, s=(0.8, 1.2)):
        ok = np.argwhere(where)
        for i, j in ok[self.rng.choice(len(ok), size=min(count, len(ok)), replace=False)]:
            getattr(self, kind)(
                float(self.X[i, j]), float(self.Z[i, j]), float(self.rng.uniform(*s))
            )


def sample(a, R, x, z):
    n = a.shape[0]
    i = (z + R) / (2 * R) * (n - 1)
    j = (x + R) / (2 * R) * (n - 1)
    return ndi.map_coordinates(a, [i, j], order=1, mode="nearest")


def render_land(name, land, dock):
    R, h = land.R, land.h
    surf = np.maximum(h, 0)
    ymax = float(h.max()) + 0.05
    right, up, fwd = (
        np.array([1.0, 0, 0], F),
        np.array([0, COS, -SIN], F),
        np.array([0, -SIN, -COS], F),
    )
    u0, u1 = -R, R
    v0, v1 = -R * SIN - 0.02, R * SIN + ymax * COS + 0.04
    W, H = int((u1 - u0) * PXI) * SS, int((v1 - v0) * PXI) * SS
    U, V = np.meshgrid(
        (u0 + (np.arange(W) + 0.5) / (PXI * SS)).astype(F),
        (v1 - (np.arange(H) + 0.5) / (PXI * SS)).astype(F),
    )
    orig = U.reshape(-1, 1) * right + V.reshape(-1, 1) * up - fwd * 12.0
    N = len(orig)
    t = (orig[:, 1] - ymax) / SIN
    t_end = orig[:, 1] / SIN
    step = 0.006
    live = np.arange(N)
    done_t = t_end.copy()
    prev_gap = np.full(N, 1.0, F)
    for _ in range(int((ymax / SIN) / step) + 4):
        if not len(live):
            break
        P = orig[live] + fwd * t[live, None]
        gap = P[:, 1] - sample(surf, R, P[:, 0], P[:, 2])
        got = gap <= 0
        # Step back to where the ray crossed the surface.
        frac = prev_gap[live[got]] / np.maximum(prev_gap[live[got]] - gap[got], 1e-6)
        done_t[live[got]] = t[live[got]] - step * (1 - np.clip(frac, 0, 1))
        prev_gap[live] = gap
        t[live] += step
        live = live[~got & (t[live] < t_end[live])]
    P = orig + fwd * done_t[:, None]
    x, z = P[:, 0], P[:, 2]
    inside = (np.abs(x) < R) & (np.abs(z) < R)
    hh = sample(h, R, x, z)
    y = np.maximum(P[:, 1], 0)
    cell = 2 * R / (land.n - 1)
    gz, gx = np.gradient(ndi.gaussian_filter(surf, 0.8), cell)
    nrm = np.stack([-sample(gx, R, x, z), np.ones(N, F), -sample(gz, R, x, z)], axis=1)
    nrm /= np.linalg.norm(nrm, axis=1, keepdims=True)
    i = np.clip(np.round((z + R) / (2 * R) * (land.n - 1)).astype(int), 0, land.n - 1)
    j = np.clip(np.round((x + R) / (2 * R) * (land.n - 1)).astype(int), 0, land.n - 1)
    mat, base, aux = land.mat[i, j], land.base[i, j], land.aux[i, j]
    rel = y - base
    # Round towers: light their sides as the columns they are, not from the blurred terrain.
    round_side = np.zeros(N, bool)
    for tx, tz, tr in land.towers:
        near = (
            (np.hypot(x - tx, z - tz) < tr * 1.35)
            & ((mat == TOWER) | (mat == STONE))
            & (rel < aux - 0.006)
        )
        d = np.stack([x - tx, np.zeros(N, F), z - tz], axis=1)
        d /= np.linalg.norm(d, axis=1, keepdims=True) + 1e-6
        nrm = np.where(near[:, None], d, nrm)
        round_side |= near

    # Shadows: walk each point toward the sun over the heightfield.
    sh = np.ones(N, F)
    Q = P + np.array([0, 0.012, 0], F) + nrm * np.array([0.03, 0, 0.03], F)
    for k in range(1, 70):
        s = 0.012 * k * (1 + k * 0.02)
        q = Q + SUN_W.astype(F) * s
        gap = q[:, 1] - sample(surf, R, q[:, 0], q[:, 2])
        sh = np.minimum(sh, np.clip(0.5 + gap * 14 / s**0.5, 0, 1))
    sh = np.where(round_side, np.maximum(sh, 0.9), sh)
    ao = np.clip(0.55 + 0.45 * (y - sample(ndi.gaussian_filter(surf, 9), R, x, z)) / 0.08, 0.35, 1)

    rng = np.random.default_rng(7)
    tex = sample(noise(rng, land.n, 2.2), R, x, z)
    tex2 = sample(noise(rng, land.n, 14), R, x, z)
    slope = 1 - nrm[:, 1]
    alb = np.zeros((N, 3), F)
    sand = np.array([0.90, 0.78, 0.52], F) * (0.94 + 0.08 * tex)[:, None]
    grass = (
        np.array([0.30, 0.52, 0.20], F)[None, :] * (1 + 0.16 * tex2[:, None])
        + np.array([0.10, 0.06, -0.02], F)[None, :] * tex[:, None]
    )
    rock = np.array([0.46, 0.43, 0.41], F) * (0.9 + 0.2 * tex)[:, None]
    g = np.where((hh < land.sand)[:, None], sand, grass)
    g = np.where((slope > 0.32)[:, None] & (hh > land.sand * 0.8)[:, None], rock, g)
    if land.snow:
        g = np.where(
            (hh > land.snow + 0.05 * tex)[:, None] & (slope < 0.6)[:, None],
            np.array([0.93, 0.95, 0.99], F),
            g,
        )
    if land.dark:
        g = np.where(
            (hh > land.sand)[:, None],
            np.array([0.25, 0.20, 0.18], F) * (0.85 + 0.3 * tex)[:, None],
            g,
        )
    alb[:] = g
    alb[mat == TREE] = np.array([0.20, 0.44, 0.17], F)
    alb[mat == PINE] = np.array([0.11, 0.30, 0.17], F)
    alb[mat == PALM] = np.array([0.26, 0.52, 0.20], F)
    for m in (TREE, PINE, PALM):
        alb[mat == m] *= (0.86 + 0.3 * tex[mat == m])[:, None]
    alb[mat == WOOD] = np.array([0.42, 0.29, 0.18], F)
    alb[mat == STONE] = np.array([0.62, 0.60, 0.56], F)
    walls = mat == WALL
    wall_h = aux % 1.0
    tint = np.floor(aux).astype(int)
    roofs = np.array([[0.72, 0.30, 0.20], [0.78, 0.56, 0.24], [0.36, 0.44, 0.58]], F)
    alb[walls] = np.where(
        (rel[walls] > wall_h[walls] + 0.004)[:, None],
        roofs[tint[walls] % 3],
        np.array([0.90, 0.85, 0.74], F),
    )
    emit = np.zeros(N, F)
    # Lit windows on the walls that face the camera.
    win = (
        walls
        & (rel < wall_h * 0.78)
        & (rel > wall_h * 0.3)
        & (nrm[:, 2] > 0.5)
        & (np.abs(((x * 26) % 1.0) - 0.5) < 0.22)
    )
    emit[win] = 1.0
    tw = mat == TOWER
    band = ((rel / np.maximum(aux, 1e-3)) * 5).astype(int) % 2 == 0
    alb[tw] = np.where(
        band[tw][:, None], np.array([0.80, 0.22, 0.16], F), np.array([0.94, 0.92, 0.86], F)
    )
    lantern = tw & (rel > aux * 0.86)
    alb[lantern] = (1.0, 0.9, 0.6)
    emit[lantern] = 1.5
    lv = mat == LAVA
    alb[lv] = (1.0, 0.45, 0.1)
    emit[lv] = 1.3

    ndl = np.clip(nrm @ SUN_W.astype(F), 0, 1)
    light = SUN_C * (ndl * sh)[:, None] + SKY_C * ((0.55 + 0.45 * nrm[:, 1]) * ao)[:, None] * 0.6
    col = alb * light + emit[:, None] * np.array([1.5, 1.05, 0.5], F)
    alpha = np.ones(N, F)

    # Water: clear and green over the shallows, fading to nothing where it gets deep.
    wet = hh <= 0
    depth = np.clip(-hh, 0, None)
    rip = sample(noise(rng, land.n, 1.6), R, x, z)
    shallow = np.exp(-depth / 0.085)
    wcol = (
        np.array([0.10, 0.50, 0.55], F)[None, :] * (1 - shallow[:, None])
        + np.array([0.46, 0.86, 0.80], F)[None, :] * shallow[:, None]
    )
    wcol = wcol * (0.5 + 0.5 * sh[:, None]) + 0.06 * rip[:, None]
    surfl = np.clip(smooth(0.016, 0.0, depth) * (0.75 + 0.5 * rip), 0, 1)
    surfl = np.maximum(
        surfl, np.clip(np.exp(-(((depth - 0.045) / 0.008) ** 2)) * (0.25 + 0.9 * rip), 0, 0.7)
    )
    wcol = wcol * (1 - surfl[:, None]) + np.array([0.94, 0.98, 1.0], F)[None, :] * surfl[:, None]
    walpha = np.clip(np.maximum(shallow * 0.88, surfl), 0, 1) * smooth(0.36, 0.2, depth)
    col = np.where(wet[:, None], wcol, col)
    alpha = (
        np.where(wet, walpha * smooth(0.98, 0.7, np.sqrt(x * x + (z * 1.25) ** 2) / R), alpha)
        * inside
    )
    # Sand darkens where the sea has just been.
    col = np.where(((hh > 0) & (hh < 0.012))[:, None], col * 0.82, col)

    rgb, alpha, glow = col.reshape(H, W, 3), alpha.reshape(H, W), emit.reshape(H, W)
    for lx, ly, lz, power in land.lamps:
        px = (lx - u0) * PXI * SS
        py = (v1 - (ly * COS - lz * SIN)) * PXI * SS
        yy, xx = np.ogrid[:H, :W]
        glow = glow + power * np.exp(-(((xx - px) ** 2 + (yy - py) ** 2) / (2 * (2.2 * SS) ** 2)))
    bloom = (
        ndi.gaussian_filter(glow, 10 * SS) * 7.0
        + ndi.gaussian_filter(glow, 3 * SS) * 1.6
        + np.clip(glow, 0, 1) * 0.8
    )
    rgb = (
        rgb * alpha[..., None]
        + np.array([1.0, 0.72, 0.32], F) * np.clip(bloom, 0, 1.3)[..., None] * 0.85
    )
    alpha = np.clip(alpha + np.clip(bloom, 0, 1) * 0.7, 0, 1)
    rgb = np.where(alpha[..., None] > 1e-4, rgb / np.maximum(alpha[..., None], 1e-4), 0)
    out = finish(rgb, alpha, SS)
    # For the Blender pass: the water alone (the land comes from Blender), and what to build.
    os.makedirs(STAGE, exist_ok=True)
    wet2 = wet.reshape(H, W)
    finish(np.where(wet2[..., None], rgb, 0), np.where(wet2, alpha, 0), SS).save(
        os.path.join(STAGE, f"{name}-water.png")
    )
    g = land.ground
    gz2, gx2 = np.gradient(ndi.gaussian_filter(np.maximum(g, 0), 1.2), cell)
    steep = 1 - 1 / np.sqrt(1 + gx2**2 + gz2**2)
    rng2 = np.random.default_rng(7)
    t1, t2 = noise(rng2, land.n, 2.2), noise(rng2, land.n, 14)
    cmap = np.where(
        (g < land.sand)[..., None],
        np.array([0.86, 0.72, 0.46], F) * (0.94 + 0.08 * t1)[..., None],
        np.array([0.22, 0.42, 0.11], F)[None, None, :] * (1 + 0.22 * t2[..., None])
        + np.array([0.05, 0.03, -0.01], F)[None, None, :] * t1[..., None],
    )
    cmap = np.where(
        ((steep > 0.3) & (g > land.sand * 0.8))[..., None],
        np.array([0.36, 0.33, 0.30], F) * (0.9 + 0.2 * t1)[..., None],
        cmap,
    )
    if land.snow:
        cmap = np.where(
            ((g > land.snow + 0.05 * t1) & (steep < 0.62))[..., None],
            np.array([0.85, 0.88, 0.95], F),
            cmap,
        )
    if land.dark:
        cmap = np.where(
            (g > land.sand)[..., None],
            np.array([0.13, 0.095, 0.08], F) * (0.85 + 0.3 * t1)[..., None],
            cmap,
        )
    np.savez_compressed(
        os.path.join(STAGE, f"{name}.npz"), ground=g, colour=np.clip(cmap, 0, 1).astype(F)
    )
    json.dump(
        dict(R=R, vc=float((v0 + v1) / 2), w=out.width, h=out.height, objs=land.objs),
        open(os.path.join(STAGE, f"{name}.json"), "w"),
    )
    out.save(os.path.join(STAGE, f"{name}-numpy.png"))

    def pix(p):
        return [
            round(float((p[0] - u0) * PXI), 1),
            round(float((v1 - (p[1] * COS - p[2] * SIN)) * PXI), 1),
        ]

    meta = dict(
        w=out.width,
        h=out.height,
        at=pix((0, 0, 0)),
        dock=pix((dock[0], 0, dock[1])),
        top=pix((0, ymax, 0))[1],
    )
    meta["lamps"] = [pix((lx, ly, lz)) for lx, ly, lz, _ in land.lamps]
    return name, meta


def make(kind):
    seeds = dict(peak=3, palms=8, fort=14, volcano=21, pines=29, harbour=40)
    if kind == "harbour":
        land = Land(2.35, 760, seeds[kind])
        inl = land.shape(1.75, 1.25, 0.16)
    else:
        land = Land(1.55, 560, seeds[kind])
        inl = land.shape(1.12, 0.84, 0.2)
    land.snow, land.dark, land.sand = 0, False, 0.035
    rng = land.rng
    n3 = noise(rng, land.n, land.n / 40)
    up = np.clip(inl, 0, None)
    # Seabed shelving away, then a beach, then the ground rising behind it.
    h = np.where(inl > 0, 0.012 + 0.03 * np.tanh(up * 9), inl * 0.42)
    X, Z = land.X, land.Z
    if kind == "peak":
        r = np.sqrt((X - 0.1) ** 2 + ((Z + 0.1) / 0.8) ** 2)
        ridge = 1 - np.abs(noise(rng, land.n, land.n / 22))
        h += (
            smooth(0.12, 0.45, up) * (0.08 + 0.03 * n3)
            + 0.78 * np.exp(-((r / 0.36) ** 2)) * (0.7 + 0.3 * ridge)
            + 0.3 * np.exp(-(((X + 0.42) ** 2 + (Z + 0.05) ** 2) / 0.05)) * (0.7 + 0.3 * ridge)
        )
        land.snow = 0.5
    elif kind == "palms":
        h += smooth(0.25, 0.6, up) * (0.035 + 0.02 * n3)
        land.sand = 0.06
    elif kind == "fort":
        h += smooth(0.1, 0.4, up) * (0.10 + 0.03 * n3) + 0.1 * smooth(0.45, 0.7, up)
    elif kind == "volcano":
        r = np.sqrt((X - 0.05) ** 2 + ((Z + 0.08) / 0.85) ** 2)
        cone = 0.86 * np.clip(1 - r / 0.72, 0, 1) ** 1.25
        h += smooth(0.08, 0.3, up) * 0.05 + np.minimum(
            cone, 0.62 - 2.2 * np.clip(0.12 - r, 0, 1)
        ) * (0.92 + 0.08 * n3)
        land.dark = True
    elif kind == "pines":
        h += smooth(0.1, 0.45, up) * (0.12 + 0.07 * n3) + 0.16 * np.exp(
            -(((X + 0.25) ** 2 + (Z + 0.1) ** 2) / 0.12)
        )
    else:
        r = np.sqrt((X + 0.85) ** 2 + ((Z + 0.25) / 0.8) ** 2)
        ridge = 1 - np.abs(noise(rng, land.n, land.n / 22))
        h += (
            smooth(0.1, 0.4, up) * (0.09 + 0.03 * n3)
            + 0.5 * np.exp(-((r / 0.36) ** 2)) * (0.75 + 0.25 * ridge)
            + 0.2
            * smooth(0.2, 0.0, np.sqrt((X - 1.18) ** 2 + (Z + 0.25) ** 2) / 1.4)
            * (inl > 0.08)
        )
    land.h = h.astype(F)
    land.ground = land.h.copy()
    green = (inl > 0.2) & (h > land.sand + 0.01)
    if kind == "peak":
        land.scatter("pine", 90, green & (h < 0.3))
        land.house(0.55, 0.3, tint=0)
        dock = (0.25, 1.02)
    elif kind == "palms":
        land.scatter("palm", 26, (inl > 0.14))
        land.house(-0.05, 0.05, tint=1)
        land.house(0.3, 0.2, 0.09, 0.07, tint=1)
        dock = (0.3, 0.95)
    elif kind == "fort":
        for dx, dz, w, d in (
            (0, -0.2, 0.34, 0.035),
            (0, 0.2, 0.34, 0.035),
            (-0.3, 0, 0.035, 0.22),
            (0.3, 0, 0.035, 0.22),
        ):
            land.block(0.05 + dx, -0.12 + dz, w, d, 0.15)
        for dx in (-0.3, 0.3):
            for dz in (-0.2, 0.2):
                land.tower(0.05 + dx, -0.12 + dz, 0.075, 0.24, STONE)
        land.block(0.05, -0.14, 0.12, 0.09, 0.22)
        land.scatter("tree", 46, green & ((np.abs(X - 0.05) > 0.45) | (np.abs(Z + 0.12) > 0.34)))
        land.lamps.append((0.05, land.at(0.05, 0.1) + 0.1, 0.1, 0.8))
        dock = (0.1, 0.98)
    elif kind == "volcano":
        m = np.sqrt((X - 0.05) ** 2 + ((Z + 0.08) / 0.85) ** 2) < 0.085
        land.mat[m] = LAVA
        land.objs.append(dict(kind="lava", x=0.05, z=-0.08, r=0.085, g=float(land.h[m].mean())))
        land.scatter("tree", 30, green & (h < 0.12))
        land.scatter("palm", 8, (inl > 0.12) & (h < 0.06))
        dock = (0.2, 1.0)
    elif kind == "pines":
        land.scatter("pine", 190, green)
        land.house(0.42, 0.34, tint=2)
        dock = (0.3, 0.98)
    else:
        town = [
            (-0.2, 0.2, 0),
            (0.08, 0.05, 1),
            (0.36, 0.26, 0),
            (0.62, 0.06, 2),
            (0.14, 0.42, 1),
            (-0.42, 0.44, 2),
            (0.66, 0.42, 0),
            (-0.05, -0.2, 2),
            (0.4, -0.16, 1),
        ]
        for hx, hz, tint in town:
            land.house(hx, hz, tint=tint)
        clear = np.ones_like(green)
        for hx, hz, _ in town:
            clear &= (np.abs(X - hx) > 0.2) | (np.abs(Z - hz) > 0.17)
        land.scatter("tree", 70, green & clear & (X < 0.9) & (h < 0.22))
        land.scatter("pine", 30, green & clear & (X < -0.5) & (h < 0.4))
        land.tower(1.22, -0.22, 0.09, 0.85)
        land.lamps.append((1.22, land.at(1.22, -0.22) + 0.02, -0.22, 0.0))
        land.jetty(0.2, 0.95, 0.5)
        land.jetty(0.75, 0.82, 0.46)
        dock = (0.5, 1.5)
    if kind != "harbour":
        # Find the south shore under the dock and run a jetty out from it.
        col = np.argmin(np.abs(land.X[0] - dock[0]))
        rows = np.nonzero(land.h[:, col] > 0.01)[0]
        shore = float(land.Z[rows.max(), 0]) if len(rows) else 0.6
        land.jetty(dock[0], shore - 0.06, 0.36)
        dock = (dock[0] + 0.02, shore + 0.5)
    return render_land("harbour" if kind == "harbour" else f"isle-{kind}", land, dock)


# ------------------------------------------------------------- the sea


def periodic(rng, n, lo, hi):
    """Smooth noise that tiles: white noise, band-passed in the frequency domain."""
    f = np.fft.fftfreq(n) * n
    r = np.sqrt(f[None, :] ** 2 + f[:, None] ** 2)
    spec = (
        np.fft.fft2(rng.standard_normal((n, n))) * ((r >= lo) & (r <= hi)) / np.maximum(r, 1) ** 1.2
    )
    a = np.real(np.fft.ifft2(spec))
    return (a / np.abs(a).max()).astype(F)


def sea():
    """The open sea, three moments of it (so the chart can melt one into the next), its
    glitter, and long swell to roll across. Every layer tiles."""
    n = 1024
    rng = np.random.default_rng(11)
    ax = np.arange(n, dtype=F) / n
    X, Y = np.meshgrid(ax, ax)
    warp = periodic(rng, n, 1, 4) * 0.03
    warp2 = periodic(rng, n, 2, 7) * 0.02
    # Where the wind is on the water: ruffled patches and glassy slicks.
    wind = smooth(-0.35, 0.45, periodic(rng, n, 1, 3) + 0.35 * periodic(rng, n, 3, 9))
    chop1, chop2, chop3 = (
        periodic(rng, n, 24, 70),
        periodic(rng, n, 70, 200),
        periodic(rng, n, 9, 24),
    )
    streak = np.fft.fft2(rng.standard_normal((n, n)))
    f = np.fft.fftfreq(n) * n
    fx, fy = np.meshgrid(f, f)
    # Foam lies in streaks down the wind (which blows toward the upper right).
    along, across = fx * 0.8 - fy * 0.6, fx * 0.6 + fy * 0.8
    streak = np.real(
        np.fft.ifft2(
            streak * np.exp(-((along / 26) ** 2) - (across / 110) ** 2) * (np.hypot(fx, fy) > 6)
        )
    )
    streak = (streak / np.abs(streak).max()).astype(F)
    # Trains of swell: whole waves per tile, so it repeats; sharpened so crests are narrow and
    # troughs wide.
    trains = (
        (3, 5, 1.0, 0.3),
        (5, 8, 0.62, 1.9),
        (2, 7, 0.55, 4.0),
        (7, 4, 0.3, 2.9),
        (9, 14, 0.34, 2.2),
        (15, 11, 0.2, 5.1),
        (6, 19, 0.2, 0.8),
    )
    sun = np.array([-0.5, 0.42, -0.55], F)
    sun /= np.linalg.norm(sun)
    half = sun + np.array([0, SIN, COS], F)
    half /= np.linalg.norm(half)
    deep, mid, lit = (
        np.array([0.012, 0.058, 0.100], F),
        np.array([0.026, 0.128, 0.172], F),
        np.array([0.060, 0.265, 0.300], F),
    )
    sky = np.array([0.20, 0.32, 0.42], F)
    frames = 3
    for k in range(frames):
        turn = 2 * np.pi * k / frames
        hgt = np.zeros((n, n), F)
        for kx, ky, amp, ph in trains:
            th = 2 * np.pi * (kx * (X + warp) + ky * (Y + warp2)) + ph + turn
            hgt += amp * (1 - 2 * np.abs(np.sin(th / 2)) ** 1.35)
        hgt += (0.25 + 0.95 * wind) * (0.8 * chop1 + 0.45 * chop2) + 0.5 * chop3
        gy, gx = np.gradient(hgt)
        nrm = np.dstack([-gx * 5.0, np.ones_like(gx), -gy * 5.0])
        nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
        facing = np.clip(nrm @ sun, 0, 1)
        spec = np.clip(nrm @ half, 0, 1)
        crest = smooth(0.55, 2.1, hgt)
        body = (0.5 + 0.5 * periodic(np.random.default_rng(3), n, 1, 4))[..., None]
        col = deep * (1 - body) + mid * body
        col = col * (0.82 + 0.18 * wind[..., None])  # slicks lie darker
        col = (
            col + (lit - mid) * (smooth(0.4, 0.97, facing) * 0.34 + crest * 0.3)[..., None]
        )  # light through the crests
        col = col + sky * ((1 - nrm[..., 1]) ** 2 * 1.6)[..., None]  # sky on the steep faces
        col = (
            col
            + np.array([0.80, 0.84, 0.76], F) * (spec**90 * (0.12 + 0.16 * (1 - wind)))[..., None]
        )
        # Whitecaps where the wind is up and a crest is breaking.
        cap = (
            smooth(0.34, 0.75, streak * 2.4 + 0.28)
            * smooth(0.95, 2.3, hgt)
            * smooth(0.45, 0.9, wind)
        )
        col = (
            col * (1 - cap[..., None] * 0.85)
            + np.array([0.86, 0.93, 0.95], F) * cap[..., None] * 0.85
        )
        Image.fromarray((np.clip(col, 0, 1) ** (1 / 2.2) * 255 + 0.5).astype(np.uint8), "RGB").save(
            os.path.join(IMG, f"sea-{k}.png"), optimize=True
        )
        if k == 0:
            a = np.clip(ndi.gaussian_filter(spec**240, 0.6, mode="wrap") * 3.2, 0, 1)
            glint = np.dstack(
                [np.full((n, n), 1.0, F), np.full((n, n), 0.96, F), np.full((n, n), 0.84, F), a]
            )
            Image.fromarray((glint * 255 + 0.5).astype(np.uint8), "RGBA").save(
                os.path.join(IMG, "glint.png"), optimize=True
            )
    # Long swell to roll across the chart: soft bright backs, soft dark troughs.
    th = 2 * np.pi * (2 * (X + warp * 2.2) + 3 * (Y + warp2 * 2.2))
    roll = (1 - 2 * np.abs(np.sin(th / 2)) ** 1.6) + 0.35 * periodic(rng, n, 2, 6)
    bright = smooth(0.25, 1.0, roll) * 0.2
    dark = smooth(-0.2, -1.0, roll) * 0.26
    a = np.maximum(bright, dark)
    tone = np.where(
        (bright >= dark)[..., None], np.array([0.62, 0.88, 0.90], F), np.array([0.0, 0.03, 0.06], F)
    )
    Image.fromarray((np.dstack([tone, a]) * 255 + 0.5).astype(np.uint8), "RGBA").save(
        os.path.join(IMG, "roll.png"), optimize=True
    )
    return "sea", dict(w=n, h=n, frames=frames)


def clouds():
    n = 640
    rng = np.random.default_rng(5)
    a = periodic(rng, n, 1, 4) + 0.55 * periodic(rng, n, 4, 12) + 0.25 * periodic(rng, n, 12, 40)
    cover = smooth(0.25, 0.95, a)
    alpha = cover * 0.9
    shade = 0.82 + 0.18 * smooth(0.3, 1.2, a)
    arr = np.dstack([shade, shade * 1.0, np.minimum(shade * 1.04, 1), alpha])
    Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA").save(
        os.path.join(IMG, "clouds.png"), optimize=True
    )
    return "clouds", dict(w=n, h=n)


FLAGS = [
    (239, 106, 90),
    (63, 199, 189),
    (244, 194, 78),
    (176, 140, 240),
    (246, 241, 228),
    (143, 217, 110),
    (255, 157, 92),
    (111, 180, 255),
]


def flags():
    """A pennant for each ship, streaming aft from the pole at its right (or left) edge."""
    s = 8
    w, h = 26 * s, 14 * s
    for i, colour in enumerate(FLAGS):
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        steps = 40
        for k in range(steps):
            x0, x1 = w - (k + 1) * w / steps * 0.94, w - k * w / steps * 0.94
            u = k / steps
            wave = math.sin(u * 7.5) * 1.6 * s * u
            half = (5.2 - 2.6 * u) * s
            mid = h / 2 + wave
            lum = 0.78 + 0.26 * math.sin(u * 7.5 + 1.2)
            c = tuple(int(min(255, v * lum)) for v in colour)
            d.polygon(
                [
                    (x0, mid - half * 0.96),
                    (x1 + 1, mid - half),
                    (x1 + 1, mid + half),
                    (x0, mid + half * 0.96),
                ],
                fill=c + (255,),
            )
        d.polygon([(0, 0), (9 * s, h / 2 + math.sin(6.5) * 1.4 * s), (0, h)], fill=(0, 0, 0, 0))
        im = im.resize((w // s * 2, h // s * 2), Image.LANCZOS)
        im.save(os.path.join(IMG, f"flag-{i}-r.png"))
        im.transpose(Image.FLIP_LEFT_RIGHT).save(os.path.join(IMG, f"flag-{i}-l.png"))
    return "flag", dict(w=52, h=28)


def job(what):
    if what == "sea":
        return sea()
    if what == "clouds":
        return clouds()
    if what == "flags":
        return flags()
    return make(what)


if __name__ == "__main__":
    # The old goal islands (peak ... harbour) are no longer drawn; name them to rebuild them.
    kinds = ["sea", "clouds", "flags"]
    todo = [a.replace("isle-", "") for a in sys.argv[1:]] or kinds
    with Pool(min(len(todo), 9)) as pool:
        meta = dict(pool.map(job, todo, chunksize=1))
    path = os.path.join(HERE, "world.json")
    old = json.load(open(path)) if os.path.exists(path) else {}
    old.update(meta)
    json.dump(old, open(path, "w"), indent=1)
    print(json.dumps(meta))
