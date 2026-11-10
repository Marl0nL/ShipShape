#!/usr/bin/env python3
"""Seamless sheets of the repeating sea and cloud images: the chart moves each as one image,
because tiles laid side by side show their seams when moved by fractions of a pixel.

    python3 art/sheets.py [IMG_DIR]   (default: ../img)
"""

import os
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
IMG = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "img")


def mosaic(src, tile, nx, ny, out):
    im = (
        Image.open(os.path.join(IMG, f"{src}.png"))
        .convert("RGBA")
        .resize((tile, tile), Image.LANCZOS)
    )
    sheet = Image.new("RGBA", (tile * nx, tile * ny))
    for j in range(ny):
        for i in range(nx):
            sheet.paste(im, (i * tile, j * tile))
    sheet.save(os.path.join(IMG, f"{out}.png"), optimize=True)


if __name__ == "__main__":
    mosaic("roll", 512, 6, 3, "roll-sheet")
    mosaic("clouds", 640, 3, 2, "clouds-sheet")
    mosaic("clouds-shadow", 640, 3, 2, "clouds-shadow-sheet")
    print("sheets")
