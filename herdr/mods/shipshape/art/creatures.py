#!/usr/bin/env python3
"""Small painted pieces the chart animates: gulls (wings up, wings down, and their
shadow), a chimney's plume of smoke leaning downwind, and the lighthouse's beam
(drawn round its centre, to be spun).

    python3 art/creatures.py [OUT_DIR]   (default: ../img)
"""

import os
import sys

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "img")
S = 8  # supersampling


def gull(lift):
    """A gull seen from above and behind: body, two wings bent at the wrist, grey backs, dark tips.
    lift: how far the wings are raised (1 up, -0.6 down)."""
    w, h = 44 * S, 20 * S
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    cx, cy = w / 2, h * 0.58
    for side in (-1, 1):
        wrist = (cx + side * 9 * S, cy - lift * 5 * S)
        tip = (cx + side * 20 * S, cy - lift * 3 * S + 4 * S)
        lead = [(cx, cy - 1.2 * S), wrist, tip]
        trail = [
            tip,
            (wrist[0] + side * 1 * S, wrist[1] + 3.2 * S),
            (cx + side * 2 * S, cy + 2.4 * S),
        ]
        d.polygon(lead + trail, fill=(214, 220, 226, 255))
        d.polygon(
            [wrist, tip, (wrist[0] + side * 4 * S, wrist[1] + 2.2 * S)], fill=(52, 56, 62, 255)
        )
        d.line([(cx, cy - 1.2 * S), wrist, tip], fill=(250, 251, 252, 255), width=int(1.1 * S))
    d.ellipse((cx - 2.2 * S, cy - 3.6 * S, cx + 2.2 * S, cy + 4.2 * S), fill=(246, 247, 248, 255))
    d.ellipse((cx - 1.5 * S, cy - 5.2 * S, cx + 1.5 * S, cy - 2.2 * S), fill=(250, 250, 250, 255))
    return im.resize((w // S * 2, h // S * 2), Image.LANCZOS)


def shadow(im):
    a = np.asarray(im)[..., 3].astype(np.float32) / 255
    a = ndi.gaussian_filter(a, 1.6) * 0.45
    out = np.zeros(a.shape + (4,), np.uint8)
    out[..., 2] = 18
    out[..., 3] = (a * 255).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def plume():
    """Smoke from a chimney: thick and grey at the foot, thinning and bending away east as it
    rises."""
    w, h = 72, 120
    rng = np.random.default_rng(5)
    ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
    up = 1 - ys / h  # 0 at the chimney, 1 at the top
    lean = 10 + up**1.6 * 42  # the column's centre, drifting east with height
    wobble = ndi.gaussian_filter(rng.standard_normal(h), 4)[:, None] * 11
    width = 3 + up * 15
    core = np.exp(-(((xs - lean - wobble * up) / width) ** 2))
    billow = 0.6 + 0.4 * ndi.gaussian_filter(rng.standard_normal((h, w)), 3) / 0.25
    a = np.clip(core * billow, 0, 1) * (1 - up) ** 0.9 * np.clip(up * 9, 0, 1) * 1.25
    a = ndi.gaussian_filter(a, 1.2)
    grey = 190 + 40 * up
    rgba = np.dstack([grey, grey + 4, grey + 10, np.clip(a, 0, 1) * 255]).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def beam():
    """Two opposed wedges of light from the centre: the lantern's beam, to be spun round."""
    n = 240
    with open(os.path.join(OUT, "beam-spin.svg"), "w") as fh:
        fh.write(
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" '
            f'width="{n}" height="{n}"><defs>'
            '<radialGradient id="b" cx="120" cy="120" r="120" gradientUnits="userSpaceOnUse">'
            '<stop offset="0" stop-color="#fff" stop-opacity="0.95"/>'
            '<stop offset="0.35" stop-color="#fff" stop-opacity="0.45"/>'
            '<stop offset="1" stop-color="#fff" stop-opacity="0"/></radialGradient></defs>'
            '<path d="M120,120 L240,104 L240,136 Z M120,120 L0,104 L0,136 Z" '
            'fill="url(#b)"/></svg>\n'
        )


def main():
    os.makedirs(OUT, exist_ok=True)
    up, down = gull(1.0), gull(-0.6)
    up.save(os.path.join(OUT, "gull-up.png"))
    down.save(os.path.join(OUT, "gull-down.png"))
    shadow(up).save(os.path.join(OUT, "gull-shadow.png"))
    plume().save(os.path.join(OUT, "plume.png"))
    beam()
    print("ok")


if __name__ == "__main__":
    main()
