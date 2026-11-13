#!/usr/bin/env python3
"""Finishes Blender's renders into the mod's sprites:
  ships    foam along the waterline and a wake laid under each render
  islands  the shallows and surf (from render_world.py) laid under the land
  -L       the lamps-only renders turned into light you can lay over a darkened chart
and writes every sprite's size and anchor points to ../sprites.js.

    python3 art/post.py [OUT_DIR] [STAGE_DIR]
"""

import json
import math
import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from shipspec import (
    CLOSE_HEADING,
    CLOSE_PX,
    HEADINGS,
    PX,
    SAILS,
    SHIPS,
    SIN,
    bow,
    frame,
    heeled,
    helm,
    pixel,
    roll,
    sail_slots,
    smooth,
    trim,
    turned,
    waterline,
)

HERE = os.path.dirname(os.path.abspath(__file__))
# Where the finished sprites go. Point it at a staging folder and copy them into the mod in one go:
# Herdr reloads the mod on every file saved there, and a slow trickle of saves exhausts it.
IMG = os.environ.get("SHIPSHAPE_IMG", os.path.join(HERE, "..", "img"))
HOME = os.path.expanduser("~/.local/share/shipshape-art")
OUT = os.path.join(HOME, "out")
STAGE = os.path.join(HOME, "stage")
# Which sprites to (re)write: any of ships, isles, close, map. The anchor data is always written.
PARTS = set(sys.argv[1:]) or {
    "ships",
    "close",
    "map",
}  # ("isles": the old goal islands, no longer drawn)
ISLES = ["isle-peak", "isle-palms", "isle-fort", "isle-volcano", "isle-pines", "harbour"]


def load(path):
    return np.asarray(Image.open(path).convert("RGBA"), np.float32) / 255


def save(arr, path):
    Image.fromarray((np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8), "RGBA").save(
        path, optimize=True
    )


def over(top, under):
    a = top[..., 3:4] + under[..., 3:4] * (1 - top[..., 3:4])
    rgb = (
        top[..., :3] * top[..., 3:4] + under[..., :3] * under[..., 3:4] * (1 - top[..., 3:4])
    ) / np.maximum(a, 1e-5)
    return np.dstack([rgb, a])


def foam(c, n, k, w, h, PX=PX):
    """White water round the hull, seen by the sprite's camera; a wake astern when sails are set."""
    f = frame(c)
    px, py = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    u = (px - w / 2) / PX
    v = (h / 2 - py) / PX + f["vc"]
    x, z = u, -v / SIN  # where each pixel's ray meets the water
    b = bow(k)
    lx = x * b[0] + z * b[2]
    lz = -x * b[2] + z * b[0]
    d = waterline(c, lx, lz)
    rip = 0.5 + 0.5 * np.sin(lx * 31 + np.sin(lz * 23) * 2.2) * np.sin(lz * 37 + lx * 9)
    a = smooth(0.085, 0.0, d) * (0.55 + 0.45 * rip)
    if n > 0:
        L = c["L"]
        aft = np.clip(-lx - L * 0.7, 0, None)
        vee = (
            np.exp(-(((np.abs(lz) - 0.18 - aft * 0.27) / (0.05 + aft * 0.05)) ** 2))
            * np.exp(-aft * 1.4)
            * (aft > 0)
            * (0.5 + 0.5 * rip)
        )
        churn = np.exp(-((lz / (0.15 + aft * 0.1)) ** 2)) * np.exp(-aft * 1.9) * (aft > 0) * rip
        bowf = smooth(0.22, 0.0, d) * smooth(L * 0.3, L * 0.95, lx) * (0.6 + 0.4 * rip)
        a = a + 1.2 * vee + 0.9 * churn + 1.1 * bowf
    a = np.clip(a, 0, 1) * 0.88 * (d > -0.02)
    return np.dstack([np.full_like(a, 0.92), np.full_like(a, 0.97), np.ones_like(a), a]).astype(
        np.float32
    )


def lights(im, gain, spread, curve):
    """A render made in the dark, as light to lay over a darkened chart: each pixel as
    opaque as it is bright, so what the lamps touch shows in its own colours and the rest stays
    dark."""
    rgb = np.clip(im[..., :3] * im[..., 3:4] * gain, 0, 1)
    lum = rgb.max(axis=2)
    a = lum**curve
    colour = rgb / np.maximum(lum[..., None], 1e-4)
    # The flames themselves bloom a little.
    core = np.clip(lum - 0.8, 0, 1) * 5
    halo = np.clip(
        ndi.gaussian_filter(core, spread) * 1.6 + ndi.gaussian_filter(core, spread * 3) * 1.2,
        0,
        0.75,
    )
    warm = np.array([1.0, 0.70, 0.34], np.float32)
    total = 1 - (1 - a) * (1 - halo)
    colour = (colour * a[..., None] + warm * (halo * (1 - a))[..., None]) / np.maximum(
        total[..., None], 1e-4
    )
    return np.dstack([np.clip(colour, 0, 1), total])


def ships():
    meta = {}
    for name, c in SHIPS.items():
        f = frame(c)
        w, h = round(f["wu"] * PX), round(f["hu"] * PX)
        masts = None
        if c["masts"]:
            mx, mh, _ = c["masts"][c.get("main", 0)]
            # mast[heading][sails set]: she lies over more the more canvas she carries.
            masts = [
                [
                    pixel(c, turned(heeled((mx, c["free"] + mh, 0), roll(name, n, k)), k))
                    for n in range(SAILS[name] + 1)
                ]
                for k in range(HEADINGS)
            ]

        def have(token, name=name):
            return all(
                os.path.exists(os.path.join(OUT, f"{name}-{token}-{k}.png"))
                for k in range(HEADINGS)
            )

        meta[name] = dict(
            w=w,
            h=h,
            at=pixel(c, (0, 0, 0)),
            mast=masts,
            rock=have("R"),
            sway=all(have(f"{n}R") for n in range(1, SAILS[name] + 1)),
            lamps=have("LR") and all(have(f"L{n}") for n in range(SAILS[name] + 1)),
        )
        for k in range(HEADINGS if "ships" in PARTS else 0):
            for n in range(SAILS[name] + 1):
                src = os.path.join(OUT, f"{name}-{n}-{k}.png")
                if os.path.exists(src):
                    save(
                        over(load(src), foam(c, n, k, w, h)),
                        os.path.join(IMG, f"{name}-{n}-{k}.png"),
                    )
            # Twins (the other end of her roll) and lamps-only passes, heeled as she is.
            for token in (
                ["R", "LR"]
                + [f"{n}R" for n in range(1, SAILS[name] + 1)]
                + [f"L{n}" for n in range(SAILS[name] + 1)]
            ):
                src = os.path.join(OUT, f"{name}-{token}-{k}.png")
                if "ships" not in PARTS or not os.path.exists(src):
                    continue
                if token.startswith("L"):
                    save(
                        lights(load(src), 1.5, 2.5, 1.25),
                        os.path.join(IMG, f"{name}-{token}-{k}.png"),
                    )
                else:
                    n = int(token[:-1] or 0)
                    save(
                        over(load(src), foam(c, n, k, w, h)),
                        os.path.join(IMG, f"{name}-{token}-{k}.png"),
                    )
            src = os.path.join(OUT, f"{name}-L-{k}.png")
            if os.path.exists(src):
                save(lights(load(src), 1.5, 2.5, 1.25), os.path.join(IMG, f"{name}-L-{k}.png"))
    return meta


def isles():
    world = json.load(open(os.path.join(HERE, "world.json")))
    for name in ISLES if "isles" in PARTS else []:
        land = os.path.join(OUT, name + ".png")
        if not os.path.exists(land):
            continue
        water = load(os.path.join(STAGE, name + "-water.png"))
        save(over(load(land), water), os.path.join(IMG, name + ".png"))
        save(
            lights(load(os.path.join(OUT, name + "-L.png")), 0.8, 3.5, 1.8),
            os.path.join(IMG, name + "-L.png"),
        )
    return world


def close():
    """The deck view's big renders, and where the wheel and each sail are in them."""
    meta = {}
    k = CLOSE_HEADING
    nu = trim(k)["nu"]
    u = turned((-math.sin(nu), 0, math.cos(nu)), k)
    for name, c in SHIPS.items():
        f = frame(c)
        w, h = round(f["wu"] * CLOSE_PX), round(f["hu"] * CLOSE_PX)

        # helm[n], sails[n]: where they are with n sails set (she heels, so they move).
        def spot(p, n, c=c, name=name):
            return pixel(c, turned(heeled(p, roll(name, n, k)), k), CLOSE_PX)

        meta[name] = dict(
            w=w,
            h=h,
            at=spot((0, 0, 0), 0),
            helm=[spot(helm(name), n) for n in range(SAILS[name] + 1)],
            sails=[
                [
                    spot(centre, n)
                    + [
                        round(
                            hw
                            * CLOSE_PX
                            * 2
                            * (max(0.35, abs(float(u[0]))) if "rig" not in c else 0.8)
                        )
                    ]
                    for centre, hw in sail_slots(name)
                ]
                for n in range(SAILS[name] + 1)
            ],
        )
        for n in range(SAILS[name] + 1) if "close" in PARTS else []:
            src = os.path.join(OUT, "close", f"{name}-{n}-{k}.png")
            if os.path.exists(src):
                save(
                    over(load(src), foam(c, n, k, w, h, CLOSE_PX)),
                    os.path.join(IMG, f"close-{name}-{n}.png"),
                )
            twin = f"{n}R" if n else "R"
            src = os.path.join(OUT, "close", f"{name}-{twin}-{k}.png")
            if os.path.exists(src):
                save(
                    over(load(src), foam(c, n, k, w, h, CLOSE_PX)),
                    os.path.join(IMG, f"close-{name}-{twin}.png"),
                )
            src = os.path.join(OUT, "close", f"{name}-L{n}-{k}.png")
            if os.path.exists(src):
                save(lights(load(src), 1.5, 4, 1.25), os.path.join(IMG, f"close-{name}-L{n}.png"))

        def close_have(token, name=name):
            return os.path.exists(os.path.join(OUT, "close", f"{name}-{token}-{k}.png"))

        meta[name]["rock"] = all(close_have(f"{n}R" if n else "R") for n in range(SAILS[name] + 1))
        meta[name]["lamps"] = all(close_have(f"L{n}") for n in range(SAILS[name] + 1))
    return meta


def sea_map():
    """The Mediterranean: Blender's land over the shallows, and its lamps."""
    meta = json.load(open(os.path.join(HERE, "med.json")))
    land = os.path.join(OUT, "med.png")
    if "map" in PARTS and os.path.exists(land):
        save(
            over(load(land), load(os.path.join(STAGE, "med-water.png"))),
            os.path.join(IMG, "med.png"),
        )
        save(
            lights(load(os.path.join(OUT, "med-L.png")), 0.8, 3.0, 1.8),
            os.path.join(IMG, "med-L.png"),
        )
    return meta


if __name__ == "__main__":
    os.makedirs(IMG, exist_ok=True)
    ship_meta = ships()
    world = isles()
    close_meta = close()
    med = sea_map()
    with open(os.path.join(HERE, "..", "sprites.js"), "w") as fh:
        fh.write(
            "// Written by art/post.py: each sprite's size and anchor points, in its own px.\n"
        )
        fh.write(
            "// SHIP[class]: at = the waterline amidships; mast[heading] = the main masthead.\n"
        )
        fh.write("export const SHIP = " + json.dumps(ship_meta) + ";\n")
        fh.write("export const WORLD = " + json.dumps(world) + ";\n")
        fh.write(
            "// CLOSE[class]: the deck view's render: the wheel, and each sail [x, y, width]"
            " in the order they're set.\n"
        )
        fh.write("export const CLOSE = " + json.dumps(close_meta) + ";\n")
        fh.write(
            "// MAP: the Mediterranean. home and ports: sea = where ships lie, town = the town;"
            " routes[h][i] = the sea lane from home harbour h to ports[i].\n"
        )
        fh.write("export const MAP = " + json.dumps(med) + ";\n")
    print(len(os.listdir(IMG)), "files in img")
