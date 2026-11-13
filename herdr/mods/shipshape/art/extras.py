#!/usr/bin/env python3
"""Small painted pieces for the chart's weather and life: a second moment of each
pennant (to flutter between), cloud shadow, rain, and the lighthouse's beam.

    python3 art/extras.py [OUT_DIR]   (default: ../img)
"""

import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = os.path.join(HERE, "..", "img")
OUT = sys.argv[1] if len(sys.argv) > 1 else IMG
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


def pennant(colour, phase):
    """Pole at the left, streaming right, caught at one moment of its ripple."""
    s = 8
    w, h = 26 * s, 14 * s
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    steps = 40
    for k in range(steps):
        x0, x1 = w - (k + 1) * w / steps * 0.94, w - k * w / steps * 0.94
        u = k / steps
        wave = math.sin(u * 7.5 + phase) * 1.6 * s * u
        half = (5.2 - 2.6 * u) * s
        mid = h / 2 + wave
        lum = 0.78 + 0.26 * math.sin(u * 7.5 + 1.2 + phase)
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
    d.polygon([(0, 0), (9 * s, h / 2 + math.sin(6.5 + phase) * 1.4 * s), (0, h)], fill=(0, 0, 0, 0))
    return im.resize((w // s * 2, h // s * 2), Image.LANCZOS).transpose(Image.FLIP_LEFT_RIGHT)


def main():
    os.makedirs(OUT, exist_ok=True)
    for i, colour in enumerate(FLAGS):
        pennant(colour, 2.4).save(os.path.join(OUT, f"flag-{i}-l2.png"))
    # The clouds' shadow on the water: the same sheet, dark.
    c = np.asarray(Image.open(os.path.join(IMG, "clouds.png")).convert("RGBA"), np.float32) / 255
    sh = np.dstack(
        [
            np.zeros_like(c[..., 0]),
            np.full_like(c[..., 0], 0.02),
            np.full_like(c[..., 0], 0.07),
            c[..., 3] * 0.9,
        ]
    )
    Image.fromarray((sh * 255 + 0.5).astype(np.uint8), "RGBA").save(
        os.path.join(OUT, "clouds-shadow.png"), optimize=True
    )
    # Rain, slanting on the wind.
    rng = np.random.default_rng(3)
    im = Image.new("RGBA", (512, 512), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for _ in range(520):
        x, y, ln = rng.uniform(-20, 532), rng.uniform(-20, 532), rng.uniform(14, 34)
        d.line(
            [(x, y), (x - ln * 0.32, y + ln)],
            fill=(205, 225, 235, int(rng.uniform(50, 150))),
            width=2,
        )
    im.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "rain.png"), optimize=True)
    # The lighthouse's beam, thrown one way and the other.
    for name, flip in (("beam-r", ""), ("beam-l", ' transform="translate(200 0) scale(-1 1)"')):
        with open(os.path.join(OUT, name + ".svg"), "w") as fh:
            fh.write(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 60" width="200" '
                'height="60"><defs><linearGradient id="b" x1="0" y1="0" x2="1" y2="0">'
                '<stop offset="0" stop-color="#ffe9b0" stop-opacity="0.85"/>'
                '<stop offset="0.5" stop-color="#ffd98a" stop-opacity="0.3"/>'
                '<stop offset="1" stop-color="#ffd98a" stop-opacity="0"/></linearGradient></defs>'
                f'<path d="M0,30 L200,6 L200,54 Z" fill="url(#b)"{flip}/></svg>\n'
            )
    print(sorted(os.listdir(OUT))[:6], len(os.listdir(OUT)))


if __name__ == "__main__":
    main()
