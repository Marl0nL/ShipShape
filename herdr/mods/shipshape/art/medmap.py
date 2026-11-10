#!/usr/bin/env python3
"""The Mediterranean, for the chart: real coastlines (Natural Earth) turned into
terrain, colours, a water layer, the ports ships sail to, and sea routes to each
from Port Ledger (Heraklion, on Crete). Writes what blend_med.py renders from.

    python3 art/medmap.py [GEO_DIR] [STAGE_DIR]
"""

import heapq
import json
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi
from shipspec import COS, SIN

HOME = os.path.expanduser("~/.local/share/shipshape-art")
GEO = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HOME, "geo")
STAGE = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HOME, "stage")
F = np.float32

LON0, LON1, LAT0, LAT1 = -7.0, 37.5, 29.6, 46.6
KX = math.cos(
    math.radians(38)
)  # a degree of longitude, in degrees of latitude, at this sea's middle
WU, HU = (LON1 - LON0) * KX, LAT1 - LAT0  # the map in world units: one unit is a degree of latitude
RES = 64  # raster cells per unit
PM = 140  # rendered px per unit
NX, NZ = round(WU * RES), round(HU * RES)

# Where ships go. (name, lat, lon) of the town.
PORTS = [
    ("Valletta", 35.90, 14.51),
    ("Syracuse", 37.07, 15.29),
    ("Naples", 40.84, 14.25),
    ("Genoa", 44.41, 8.93),
    ("Marseille", 43.30, 5.37),
    ("Barcelona", 41.38, 2.18),
    ("Palma", 39.57, 2.65),
    ("Gibraltar", 36.14, -5.35),
    ("Algiers", 36.75, 3.06),
    ("Tunis", 36.80, 10.18),
    ("Tripoli", 32.89, 13.19),
    ("Venice", 45.44, 12.33),
    ("Dubrovnik", 42.65, 18.09),
    ("Piraeus", 37.94, 23.65),
    ("Rhodes", 36.43, 28.22),
    ("Smyrna", 38.42, 27.14),
    ("Limassol", 34.67, 33.04),
    ("Beirut", 33.90, 35.50),
    ("Alexandria", 31.20, 29.90),
    ("Antalya", 36.88, 30.70),
    ("Cagliari", 39.22, 9.12),
    ("Ajaccio", 41.92, 8.74),
    ("Benghazi", 32.12, 20.07),
    ("Thessaloniki", 40.63, 22.94),
]
LEDGER = ("Port Ledger", 35.34, 25.13)  # Heraklion
# Harbours a tag of Ships can call home (after Port Ledger): idle ships with that tag lie there.
HOME_PORTS = ["Valletta", "Limassol", "Palma", "Rhodes", "Syracuse", "Piraeus"]

# Names on the water (and three on the land), lettered across the chart.
SEAS = [
    ("Tyrrhenian Sea", 39.9, 12.3),
    ("Ionian Sea", 37.3, 18.6),
    ("Adriatic Sea", 43.3, 14.9),
    ("Aegean Sea", 38.9, 25.0),
    ("Sea of Crete", 36.05, 25.3),
    ("Libyan Sea", 33.7, 23.6),
    ("Levantine Sea", 33.6, 31.0),
    ("Balearic Sea", 40.4, 1.6),
    ("Alboran Sea", 35.95, -3.4),
    ("Ligurian Sea", 43.55, 8.9),
    ("Gulf of Sidra", 31.6, 18.4),
    ("Gulf of Lion", 42.75, 4.3),
    ("Strait of Sicily", 36.5, 12.1),
    ("Black Sea", 43.4, 33.5),
]
LANDS = [("Europa", 44.8, 1.2), ("Africa", 30.6, 6.0), ("Asia Minor", 39.2, 32.5)]


def cell(lat, lon):
    """A place's raster cell (column, row)."""
    return (lon - LON0) * KX * RES, (LAT1 - lat) * RES


def rasterize():
    img = Image.new("L", (NX, NZ), 0)
    draw = ImageDraw.Draw(img)
    for name in ("land.geojson", "minor.geojson"):
        for feat in json.load(open(os.path.join(GEO, name)))["features"]:
            g = feat["geometry"]
            polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
            for poly in polys:
                ring = np.asarray(poly[0])
                if (
                    ring[:, 0].max() < LON0 - 1
                    or ring[:, 0].min() > LON1 + 1
                    or ring[:, 1].max() < LAT0 - 1
                    or ring[:, 1].min() > LAT1 + 1
                ):
                    continue
                draw.polygon([cell(lat, lon) for lon, lat in ring], fill=255)
                for hole in poly[1:]:
                    draw.polygon([cell(lat, lon) for lon, lat in hole], fill=0)
    return np.asarray(img) > 127


def noise(rng, shape, sigma):
    a = ndi.gaussian_filter(rng.standard_normal(shape), sigma)
    return (a / np.abs(a).max()).astype(F)


def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def elevation():
    """Real heights and depths in metres on the map's raster (Mapzen terrain tiles, zoom 6)."""
    zoom, x0, y0 = 6, 30, 22
    tiles = np.zeros((5 * 256, 9 * 256), F)
    for ty in range(5):
        for tx in range(9):
            t = np.asarray(
                Image.open(os.path.join(GEO, "terr", f"{x0 + tx}-{y0 + ty}.png")).convert("RGB"), F
            )
            tiles[ty * 256 : (ty + 1) * 256, tx * 256 : (tx + 1) * 256] = (
                t[..., 0] * 256 + t[..., 1] + t[..., 2] / 256 - 32768
            )
    cols, rows = np.meshgrid(np.arange(NX, dtype=np.float64), np.arange(NZ, dtype=np.float64))
    lat = np.radians(LAT1 - rows / RES)
    lon = LON0 + cols / (RES * KX)
    n = 2**zoom * 256
    px = (lon + 180) / 360 * n - x0 * 256
    py = (1 - np.arcsinh(np.tan(lat)) / np.pi) / 2 * n - y0 * 256
    return ndi.map_coordinates(tiles, [py, px], order=1, mode="nearest").astype(F)


def rivers(shape):
    """The big rivers, as a mask to paint and a wider one to green their banks."""
    img = Image.new("L", (shape[1], shape[0]), 0)
    draw = ImageDraw.Draw(img)
    for feat in json.load(open(os.path.join(GEO, "rivers.geojson")))["features"]:
        g = feat["geometry"]
        if not g or (feat["properties"].get("scalerank") or 9) > 6:
            continue
        for line in [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]:
            pts = [
                cell(lat, lon)
                for lon, lat in line
                if LON0 - 1 < lon < LON1 + 1 and LAT0 - 1 < lat < LAT1 + 1
            ]
            if len(pts) > 1:
                draw.line(pts, fill=255, width=2)
    for feat in json.load(open(os.path.join(GEO, "lakes.geojson")))["features"]:
        g = feat["geometry"]
        if not g or (feat["properties"].get("scalerank") or 9) > 4:
            continue
        for poly in [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]:
            ring = np.asarray(poly[0])
            if (
                ring[:, 0].max() > LON0
                and ring[:, 0].min() < LON1
                and ring[:, 1].max() > LAT0
                and ring[:, 1].min() < LAT1
            ):
                draw.polygon([cell(lat, lon) for lon, lat in ring], fill=255)
    wet = np.asarray(img, F) / 255
    return ndi.gaussian_filter(wet, 0.7), np.clip(ndi.gaussian_filter(wet, 5) * 9, 0, 1)


def terrain(land):
    rng = np.random.default_rng(4)
    d_land = ndi.distance_transform_edt(land).astype(F)
    d_sea = ndi.distance_transform_edt(~land).astype(F)
    cols, rows = np.meshgrid(np.arange(NX, dtype=F), np.arange(NZ, dtype=F))
    lat = LAT1 - rows / RES
    lon = LON0 + cols / (RES * KX)
    metres = elevation()
    up = np.clip(metres, 0, None)
    # Real relief, stood up about ten times so it reads from this height.
    h = (
        0.012
        + 0.092 * (up / 1000) ** 0.85
        + 0.004 * noise(rng, land.shape, 1.2) * smooth(1.5, 8, d_land)
    )
    depth = np.where(land, 0, np.maximum(-metres, 4 * d_sea)).astype(F)  # metres of water
    h = np.where(land, h, -0.3 * smooth(0, 44, d_sea)).astype(F)

    # Colour by climate: green in the north, scrub round the sea, sand in the south and east;
    # rivers green their banks.
    t1, t2 = noise(rng, land.shape, 1.6), noise(rng, land.shape, 12)
    stream, banks = rivers(land.shape)
    arid = np.clip(
        smooth(36.6, 32.6, lat + 0.9 * t2)
        + 0.75 * smooth(34.5, 36.6, lon) * smooth(37.5, 34.5, lat),
        0,
        1,
    ) * (1 - 0.85 * banks)
    dry = np.clip(smooth(44.5, 38.5, lat + 1.2 * t2) + 0.4 * smooth(28, 33, lon), 0, 1) * (
        1 - 0.6 * banks
    )
    green = np.array([0.085, 0.21, 0.045], F)
    scrub = np.array([0.24, 0.25, 0.085], F)
    sand = np.array([0.62, 0.44, 0.22], F)
    col = green + (scrub - green) * dry[..., None]
    col = col + (sand - col) * arid[..., None]
    col = col * (1 + 0.16 * t1[..., None])
    gz, gx = np.gradient(ndi.gaussian_filter(np.maximum(h, 0), 1.0), 1 / RES)
    steep = 1 - 1 / np.sqrt(1 + gx**2 + gz**2)
    rocky = np.clip(smooth(0.1, 0.38, steep) + smooth(1100, 2600, up) * (0.6 + 0.4 * t1), 0, 1)
    rock = (
        np.array([0.25, 0.22, 0.19], F) * (0.9 + 0.2 * t1[..., None]) + 0.2 * sand * arid[..., None]
    )
    col = col + (rock - col) * rocky[..., None]
    # Snow: above the line, which stands higher the further south.
    line = 2250 + 110 * np.clip(46 - lat, 0, 14) + 160 * t1
    col = col + (np.array([0.88, 0.90, 0.95], F) - col) * smooth(0, 320, up - line)[..., None]
    col = col * (0.85 + 0.2 * smooth(0, 700, up)[..., None])  # the low ground lies a little darker
    col = col + (np.array([0.06, 0.20, 0.27], F) - col) * np.clip(stream * 1.4, 0, 1)[..., None]
    col = np.where((d_land < 1.6)[..., None], np.array([0.72, 0.58, 0.36], F), col)
    return h, np.clip(col, 0, 1).astype(F), d_land, d_sea, arid, depth


def water(d_sea, land, depth):
    """The sea's own colours, as the map's layer (raster space): green shallows over the real
    shelves, surf on every shore, and the deep basins a shade darker."""
    rng = np.random.default_rng(9)
    rip = noise(rng, land.shape, 1.2)
    shallow = np.maximum(
        np.exp(-ndi.gaussian_filter(depth, 2) / 75), 0.55 * np.exp(-(d_sea / RES) / 0.06)
    )
    col = (
        np.array([0.10, 0.50, 0.55], F)[None, None, :] * (1 - shallow[..., None])
        + np.array([0.46, 0.86, 0.80], F)[None, None, :] * shallow[..., None]
    )
    surf = np.clip(smooth(1.5, 0.2, d_sea) * (0.7 + 0.6 * rip), 0, 1)
    surf = np.maximum(
        surf, np.clip(np.exp(-(((d_sea - 3.4) / 0.7) ** 2)) * (0.1 + 0.9 * rip), 0, 0.6)
    )
    col = col * (1 - surf[..., None]) + np.array([0.94, 0.98, 1.0], F) * surf[..., None]
    a = np.clip(np.maximum(shallow * 0.8, surf), 0, 1)
    # (Heavily smoothed: the depth data comes in tiles that don't always agree at their edges.)
    deep = 0.26 * smooth(900, 3600, ndi.gaussian_filter(depth, 22))
    col = np.where((deep > a)[..., None], np.array([0.0, 0.015, 0.06], F), col)
    a = np.maximum(a, deep) * (~land)
    return np.dstack([col, a])


def sea_anchor(d_sea, lat, lon, off):
    """The nearest water at least `off` cells from any shore."""
    cx, cz = cell(lat, lon)
    ok = np.argwhere(d_sea >= off)
    i = np.argmin((ok[:, 1] - cx) ** 2 + (ok[:, 0] - cz) ** 2)
    return float(ok[i, 1]), float(ok[i, 0])


def land_spot(d_land, lat, lon):
    cx, cz = cell(lat, lon)
    ok = np.argwhere((d_land >= 2.5) & (d_land <= 7))
    i = np.argmin((ok[:, 1] - cx) ** 2 + (ok[:, 0] - cz) ** 2)
    return float(ok[i, 1]), float(ok[i, 0])


def routes(d_sea, start, goals):
    """Sea lanes from the start to each goal: shortest paths that keep off the coast."""
    k = 4
    d = d_sea[::k, ::k]
    open_ = d >= 5
    cost = 1 + 4 / (1 + d / 10)
    rows, cols = d.shape
    s = (int(start[1] // k), int(start[0] // k))
    best = np.full(d.shape, np.inf)
    prev = {}
    best[s] = 0
    heap = [(0.0, s)]
    steps = [
        (-1, 0, 1),
        (1, 0, 1),
        (0, -1, 1),
        (0, 1, 1),
        (-1, -1, 1.414),
        (-1, 1, 1.414),
        (1, -1, 1.414),
        (1, 1, 1.414),
    ]
    while heap:
        c, (i, j) = heapq.heappop(heap)
        if c > best[i, j]:
            continue
        for di, dj, w in steps:
            a, b = i + di, j + dj
            if 0 <= a < rows and 0 <= b < cols and open_[a, b]:
                nc = c + w * cost[a, b]
                if nc < best[a, b]:
                    best[a, b] = nc
                    prev[(a, b)] = (i, j)
                    heapq.heappush(heap, (nc, (a, b)))
    out = []
    for gx, gz in goals:
        g = (int(gz // k), int(gx // k))
        if not np.isfinite(best[g]):
            reach = np.argwhere(np.isfinite(best))
            g = tuple(reach[np.argmin((reach[:, 0] - g[0]) ** 2 + (reach[:, 1] - g[1]) ** 2)])
        path = [g]
        while path[-1] != s:
            path.append(prev[path[-1]])
        pts = [((j + 0.5) * k, (i + 0.5) * k) for i, j in reversed(path)]
        pts = [pts[0]] + pts[5:-4:10] + [pts[-1]] if len(pts) > 14 else pts
        for _ in range(2):  # round the corners off
            new = [pts[0]]
            for a, b in zip(pts, pts[1:], strict=False):
                new += [
                    (a[0] * 0.75 + b[0] * 0.25, a[1] * 0.75 + b[1] * 0.25),
                    (a[0] * 0.25 + b[0] * 0.75, a[1] * 0.25 + b[1] * 0.75),
                ]
            pts = new + [pts[-1]]
        out.append(pts)
    return out


def main():
    os.makedirs(STAGE, exist_ok=True)
    land = rasterize()
    h, col, d_land, d_sea, arid, depth = terrain(land)
    ymax = float(h.max()) + 0.12
    v1 = HU / 2 * SIN + ymax * COS + 0.04
    v0 = -HU / 2 * SIN - 0.02
    w, hpx = round(WU * PM), round((v1 - v0) * PM)

    def px(cx, cz, y=0.0):
        """A raster cell (and a height above the sea) as a pixel of the rendered map."""
        x, z = cx / RES - WU / 2, cz / RES - HU / 2
        return [round((x + WU / 2) * PM, 1), round((v1 - (y * COS - z * SIN)) * PM, 1)]

    def world(cx, cz):
        return [cx / RES - WU / 2, cz / RES - HU / 2]

    def hat(cx, cz):
        return float(h[int(round(cz)), int(round(cx))])

    home_sea = sea_anchor(d_sea, LEDGER[1] + 0.35, LEDGER[2], 20)
    anchors = [sea_anchor(d_sea, lat, lon, 16) for _, lat, lon in PORTS]
    names = [p[0] for p in PORTS]
    home_ports = [names.index(n) for n in HOME_PORTS]
    # Sea lanes from every home harbour to every port.
    lanes = [
        routes(d_sea, start, anchors) for start in [home_sea] + [anchors[i] for i in home_ports]
    ]
    towns = [land_spot(d_land, lat, lon) for _, lat, lon in PORTS]
    home_town = land_spot(d_land, LEDGER[1], LEDGER[2])

    # The water, squashed into the map's oblique view.
    wat = water(d_sea, land, depth)
    img = Image.fromarray((np.clip(wat, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA").resize(
        (w, round(HU * SIN * PM)), Image.LANCZOS
    )
    sheet = Image.new("RGBA", (w, hpx), (0, 0, 0, 0))
    sheet.paste(img, (0, round((v1 - HU / 2 * SIN) * PM)))
    sheet.save(os.path.join(STAGE, "med-water.png"))

    np.savez_compressed(
        os.path.join(STAGE, "med.npz"), ground=h, colour=col, arid=arid.astype(F), d_land=d_land
    )
    build = dict(
        wu=WU,
        hu=HU,
        res=RES,
        pm=PM,
        w=w,
        h=hpx,
        vc=(v0 + v1) / 2,
        home=dict(at=world(*home_town), g=hat(*home_town)),
        towns=[
            dict(name=p[0], at=world(*t), g=hat(*t)) for p, t in zip(PORTS, towns, strict=False)
        ],
    )
    json.dump(build, open(os.path.join(STAGE, "med.json"), "w"))
    meta = dict(
        w=w,
        h=hpx,
        home=dict(
            name=LEDGER[0],
            sea=px(*home_sea),
            town=px(*home_town, hat(*home_town)),
            lamp=px(*home_town, hat(*home_town) + 0.34),
        ),
        ports=[
            dict(name=p[0], sea=px(*a), town=px(*t, hat(*t)))
            for p, a, t in zip(PORTS, anchors, towns, strict=False)
        ],
        homes=[
            dict(name=LEDGER[0], port=-1, sea=px(*home_sea), town=px(*home_town, hat(*home_town)))
        ]
        + [
            dict(name=PORTS[i][0], port=i, sea=px(*anchors[i]), town=px(*towns[i], hat(*towns[i])))
            for i in home_ports
        ],
        routes=[
            [[[round(v) for v in px(x, z)] for x, z in lane] for lane in from_home]
            for from_home in lanes
        ],
        seas=[dict(name=n, at=px(*cell(lat, lon))) for n, lat, lon in SEAS],
        lands=[dict(name=n, at=px(*cell(lat, lon), 0.1)) for n, lat, lon in LANDS],
    )
    json.dump(meta, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "med.json"), "w"))
    Image.fromarray((np.clip(col, 0, 1) * land[..., None] * 255).astype(np.uint8)).save(
        os.path.join(STAGE, "med-colour.png")
    )
    lengths = [len(lane) for lane in lanes[0]][:6]
    print(w, hpx, "land", round(float(land.mean()), 2), "lanes", lengths)


if __name__ == "__main__":
    main()
