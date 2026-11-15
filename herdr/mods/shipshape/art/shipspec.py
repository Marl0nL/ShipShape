"""The ships' measurements, shared by the Blender model (blend_ship.py) and the
post pass (post.py). numpy only, so Blender's own Python can import it.

Axes here are the chart's: x east, y up, z south (toward the camera)."""

import math

import numpy as np

EL = math.radians(36)  # the camera looks down at this angle, from the south
SIN, COS = math.sin(EL), math.cos(EL)
SUN = np.array([-0.60, 0.50, 0.62])  # where the sun stands
SUN /= np.linalg.norm(SUN)
PX = 64  # rendered px per world unit, ships
PXI = 150  # and islands
HEADINGS = 8  # k * 45 degrees, clockwise on screen from east

SHIPS = {
    "dinghy": dict(
        L=0.46, B=0.2, draft=0.10, free=0.12, castle=0.0, fore=0.0, masts=[], ports=0, crew=1
    ),
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
        crew=2,
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
        crew=3,
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
        crew=4,
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
        crew=6,
    ),
}
SAILS = {"dinghy": 0, "sloop": 2, "brig": 4, "frigate": 6, "galleon": 9}


def smooth(a, b, x):
    t = np.clip((x - a) / (b - a), 0, 1)
    return t * t * (3 - 2 * t)


def plan(c, t):
    """Half-beam along the hull, t from -1 (stern) to 1 (bow)."""
    t = np.clip(t, -1, 1)
    return c["B"] * np.where(
        t > 0, np.maximum(1 - np.abs(t) ** 2.3, 0) ** 0.62, 0.78 + 0.22 * (1 - np.abs(t) ** 3)
    )


def deck(c, t):
    """Deck height above the water along the hull."""
    t = np.clip(t, -1, 1)
    return (
        c["free"]
        + 0.07 * c["L"] * np.maximum(t, 0) ** 2
        + c["castle"] * smooth(-0.5, -0.56, t)
        + c["fore"] * smooth(0.62, 0.68, t)
    )


def waterline(c, x, z):
    """Roughly how far a point on the water (ship's frame: x forward, z abeam) is from the hull;
    < 0 inside."""
    t = x / c["L"]
    w = plan(c, t) * (0.5 + 0.5 * (c["draft"] / (c["free"] + c["draft"])) ** 0.55)
    return np.maximum(np.abs(z) - w, np.abs(x) - c["L"])


def frame(c):
    """A class's sprite: its size in world units and the height its camera aims at."""
    top = c["free"] + max([m[1] for m in c["masts"]] + [0.45]) + 0.2
    reach = c["L"] + 1.0
    lo = -reach * SIN - 0.05
    hi = top * COS + 0.3
    return dict(wu=2 * reach, hu=hi - lo, vc=(lo + hi) / 2)


def pixel(c, p, px=PX):
    """Where a world point (x, y up, z south) lands in the class's sprite, rendered at px per
    unit."""
    f = frame(c)
    w, h = round(f["wu"] * px), round(f["hu"] * px)
    return [
        round(w / 2 + p[0] * px, 1),
        round(h / 2 - ((p[1] * COS - p[2] * SIN) - f["vc"]) * px, 1),
    ]


def bow(k):
    a = k * 2 * math.pi / HEADINGS
    return np.array([math.cos(a), 0, math.sin(a)])


# ----- the close-up (the deck view): what's where on the ship, in her own frame
# (x forward, y up, z to starboard).

# The wind is the same all over the chart: a westerly (it blows toward the east, angle 0;
# angles run clockwise on screen, as headings do). Pennants, cloud and swell go with it.
WIND = 0.0


def trim(k):
    """How a ship on heading k carries her sails in that wind (her own frame: x forward, z to
    starboard).
    alpha  where the wind blows, off the bow's direction: 0 = from dead astern, +-pi = dead ahead
    lee    which side is downwind: +1 starboard, -1 port
    nu     the square sails' facing, off the bow: half-way round toward the wind, as far as the
           yards brace
    fill   how the canvas bellies: 1 drawing full, less close-hauled, negative when taken aback
    boom   how far a fore-and-aft boom is eased out to leeward
    heel   how far she lies over to leeward under full sail
    """
    a = k * 2 * math.pi / HEADINGS
    alpha = ((WIND - a + math.pi) % (2 * math.pi)) - math.pi
    lee = 1 if alpha >= 0 else -1
    nu = max(-math.radians(62), min(math.radians(62), alpha / 2))
    drive = math.cos(alpha - nu)
    fill = -0.4 if abs(alpha) > math.radians(150) else max(0.4, drive)
    return dict(
        alpha=alpha,
        lee=lee,
        nu=nu,
        fill=fill,
        boom=math.radians(80 - 75 * abs(alpha) / math.pi),
        heel=math.radians(1.5 + 8.5 * abs(math.sin(alpha)) ** 0.8),
    )


ROCK = math.radians(5.5)  # how far a ship at anchor rolls each way
SWAY = math.radians(3.5)  # how much further a ship under sail lies over at the top of each roll


def job(token):
    """A render's sail token: "3" (three sails set), "R" (at anchor, rolled the other way), "3R"
    (three
    sails, at the far end of her roll), and any of those after "L" for the lamps-only pass.
    Returns (sails set, rolled the other way?, lamps only?)."""
    lamps = token.startswith("L")
    t = token[1:] if lamps else token
    twin = t.endswith("R")
    t = t[:-1] if twin else t
    return (int(t) if t else 0), twin, lamps


def roll(name, n, k, twin=False):
    """How far she lies over in a render, n sails set on heading k; twin: her other extreme. At
    anchor she rolls from -ROCK to +ROCK; under sail she heels to leeward and rolls further over by
    SWAY. Positive is
    to starboard."""
    if n == "R":  # older callers
        n, twin = 0, True
    if not isinstance(n, int) or n == 0:
        # At anchor she sits upright (twin: rolled over, for a rocking pair, no longer drawn).
        return ROCK if twin else 0.0
    tr = trim(k)
    heel = tr["lee"] * tr["heel"] * math.sqrt(n / SAILS[name])
    return heel + (tr["lee"] * SWAY if twin else 0)


def heeled(p, r):
    """A point in her frame, with her lying over by r (about her own keel, through the
    waterline)."""
    x, y, z = p
    return (x, y * math.cos(r) - z * math.sin(r), z * math.cos(r) + y * math.sin(r))


def pace(k):
    """How well she goes on heading k: best on a reach, slow close-hauled, barely at all head to
    wind."""
    off = abs(trim(k)["alpha"])
    return (
        1.0
        if off < math.radians(50)
        else 1.15
        if off < math.radians(115)
        else 0.72
        if off < math.radians(150)
        else 0.45
    )


CLOSE_PX = 200  # px per unit of the close-up renders
CLOSE_HEADING = 1
HELM_X = {"dinghy": -0.25, "sloop": -0.84, "brig": -0.62, "frigate": -0.95, "galleon": -1.45}


def helm(name):
    """Where the wheel stands."""
    c = SHIPS[name]
    x = HELM_X[name] * (c["L"] if name == "dinghy" else 1)
    return (x, float(deck(c, x / c["L"])) + 0.14, 0.0)


def sail_slots(name, k=CLOSE_HEADING):
    """Every sail the class can set, in the order they're set: (centre, half-width along the
    yard), ship's
    frame, trimmed as on heading k."""
    tr = trim(k)
    c = SHIPS[name]
    free = c["free"]
    if not c["masts"]:
        return []
    if c.get("rig") == "sloop":
        mx, mh, _ = c["masts"][0]
        g, lee = tr["boom"], tr["lee"]
        aft = np.array([-math.cos(g), 0, lee * math.sin(g)])
        side = np.array([math.sin(g), 0, lee * math.cos(g)])
        fwd = np.array([math.cos(g * 0.4), 0, lee * math.sin(g * 0.4)])
        base = np.array([mx, 0, 0])
        main = (
            base
            + aft * 0.34
            + side * 0.09
            + np.array([0, free + 0.34 + 0.05 + 0.3 * (mh - 0.5), 0])
        )
        jib = base + fwd * 0.42 + np.array([0, free + 0.5 + 0.22 * (mh * 0.86 - 0.5) + 0.05, 0])
        return [(main, 0.3), (jib, 0.24)]
    np.array([-math.sin(tr["nu"]), 0, math.cos(tr["nu"])])
    nrm = np.array([math.cos(tr["nu"]), 0, math.sin(tr["nu"])])
    tiers = c["tiers"]
    order = sorted(range(len(c["masts"])), key=lambda m: (m != c["main"], -m))
    out = []
    for t in range(tiers):
        for m in order:
            mx, mh, hw0 = c["masts"][m]
            th = (mh - 0.3) / tiers
            y0 = free + 0.3 + th * t
            y1 = y0 + th - 0.08
            centre = (
                np.array([mx, 0, 0])
                + nrm * (0.07 + (0.17 - 0.03 * t) * tr["fill"])
                + np.array([0, (y0 + y1) / 2, 0])
            )
            out.append((centre, hw0 * (1 - 0.2 * t)))
    return out


def turned(p, k):
    """A point in the ship's frame, out in the world with her on heading k."""
    b = bow(k)
    star = np.array([-b[2], 0, b[0]])
    return np.array([p[0] * b[0] + p[2] * star[0], p[1], p[0] * b[2] + p[2] * star[2]])
