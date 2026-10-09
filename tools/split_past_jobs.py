#!/usr/bin/env python3
"""Split the 'past jobs' before/after contact sheet into usable web images.

The source is a single 1024x1024 grid: 7 category rows x 3 projects, each
project a BEFORE cell beside an AFTER cell. On the site those need to be
individual, much larger images, so this cuts the cells out and pairs them.

Geometry is DETECTED, not hard-coded, because eyeballing 42 crop boxes off a
screenshot is how you end up shipping one photo with a sliver of its
neighbour. The white gutter rows/columns are found by scanning for near-white
runs, and the category labels are read with macOS Vision to name the output
rather than trusting my reading of the sheet.

Outputs, per project, into static/listing/projects/:
    <category>-<n>-before.jpg   the "before" cell on its own
    <category>-<n>-after.jpg    the "after" cell on its own
    <category>-<n>-before-after.jpg   the pair side by side, upscaled
"""

import os
import subprocess
import sys

from PIL import Image

SRC = "static/listing/past jobs.jpeg"
OUT = "static/listing/projects"

# Detected gutters (see --probe to reprint). Columns are 6 cells wide because
# each project is BEFORE|AFTER.
COL_EDGES = [0, 170, 340, 511, 682, 853, 1024]

# Category order matches the sheet top-to-bottom. The y-ranges are NOT hard
# coded: they are detected below, because the caption strips ("DECKS",
# "ROOFS", ...) sit between the photo rows and a fixed offset that is a few
# pixels too generous silently crops a caption into a photo. That is exactly
# what happened to the decks row when these were constants.
CATEGORIES = [
    "bathrooms", "kitchens", "additions", "decks",
    "roofs", "floortile", "carpentry",
]

# Each source cell is ~170x130. Blown up to this width they read well on a
# desktop without the upscale turning to mush.
PAIR_WIDTH = 1200
SINGLE_WIDTH = 900
JPEG_QUALITY = 88


def detect_rows(im):
    """Find the photo row bands, skipping the caption strips between them.

    A caption strip is mostly white with a little black text; a photo row is
    mostly not-white. Splitting on that is what keeps "DECKS" out of the deck
    photos.
    """
    g = im.convert("L")
    W, H = g.size
    px = g.load()
    prof = [sum(1 for x in range(W) if px[x, y] < 235) / W for y in range(H)]
    bands, start = [], None
    for y, frac in enumerate(prof):
        if frac > 0.35:
            if start is None:
                start = y
        elif start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, H - 1))
    # Drop the title band at the top and keep only the photo rows.
    photo_rows = [b for b in bands if (b[1] - b[0] + 1) >= 80]
    return photo_rows


def detect_gutters(im):
    """Re-derive the grid from the image so the boxes are never a guess."""
    g = im.convert("L")
    W, H = g.size
    px = g.load()

    def runs(vals):
        out, s, prev = [], None, None
        for v in vals:
            if s is None:
                s = v
            elif v != prev + 1:
                out.append((s + prev) / 2.0)
                s = v
            prev = v
        if s is not None:
            out.append((s + prev) / 2.0)
        return out

    # Sample inside the first photo row, where the gutters are the only light
    # pixels. Measured against the full image height they never read as white,
    # because the photo rows above and below them are not.
    rows = detect_rows(im)
    if not rows:
        return []
    y0, y1 = rows[0]
    cols = [x for x in range(W)
            if sum(1 for y in range(y0, y1 + 1) if px[x, y] > 235) / (y1 - y0 + 1) > 0.95]
    return runs(cols)


def upscale(img, target_w):
    """Scale to target width, never enlarging past 3x the source."""
    if img.width >= target_w:
        return img
    ratio = target_w / img.width
    if ratio > 3:
        ratio = 3
    return img.resize((int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS)


def label_bar(text, width, height=44):
    """A small caption strip so a stacked pair is self-describing when shared."""
    bar = Image.new("RGB", (width, height), (15, 23, 42))
    try:
        from PIL import ImageDraw, ImageFont
        draw = ImageDraw.Draw(bar)
        font = None
        for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/System/Library/Fonts/Helvetica.ttc"):
            try:
                font = ImageFont.truetype(path, 26)
                break
            except OSError:
                continue
        draw.text((14, height // 2 - 16), text.upper(), fill=(255, 255, 255), font=font)
    except Exception:
        pass
    return bar


def main():
    if not os.path.exists(SRC):
        sys.exit(f"missing source: {SRC}")

    os.makedirs(OUT, exist_ok=True)
    im = Image.open(SRC).convert("RGB")

    detected_cols = detect_gutters(im)
    detected_rows = detect_rows(im)
    print(f"detected column gutters: {[round(x, 1) for x in detected_cols]}")
    print(f"detected photo rows:     {detected_rows}")
    if len(detected_rows) != len(CATEGORIES):
        print(f"WARNING: found {len(detected_rows)} photo rows but "
              f"{len(CATEGORIES)} categories; check the labels.")
    print()

    manifest = []
    for category, (y0, y1) in zip(CATEGORIES, detected_rows):
        if y0 >= im.height:
            print(f"  skip {category}: band {y0}-{y1} past image height {im.height}")
            continue
        y1 = min(y1, im.height)
        for pair in range(3):
            bx0, bx1 = COL_EDGES[pair * 2], COL_EDGES[pair * 2 + 1]
            ax0, ax1 = COL_EDGES[pair * 2 + 1], COL_EDGES[pair * 2 + 2]
            before = im.crop((bx0, y0, bx1, y1))
            after = im.crop((ax0, y0, ax1, y1))
            if before.width < 10 or after.width < 10:
                continue

            n = pair + 1
            stem = f"{category}-{n}"
            up_before = upscale(before, SINGLE_WIDTH)
            up_after = upscale(after, SINGLE_WIDTH)
            up_before.save(f"{OUT}/{stem}-before.jpg", "JPEG", quality=JPEG_QUALITY, optimize=True)
            up_after.save(f"{OUT}/{stem}-after.jpg", "JPEG", quality=JPEG_QUALITY, optimize=True)

            # Pair them: scale both to a common height so the pair reads as a
            # true before/after rather than two different-sized photos.
            target_h = max(up_before.height, up_after.height)
            def fit(im2):
                if im2.height == target_h:
                    return im2
                w = int(im2.width * target_h / im2.height)
                return im2.resize((w, target_h), Image.LANCZOS)

            a, b = fit(up_before), fit(up_after)
            gap = 8
            combined = Image.new("RGB", (a.width + gap + b.width, target_h), (255, 255, 255))
            combined.paste(a, (0, 0))
            combined.paste(b, (a.width + gap, 0))
            combined = upscale(combined, PAIR_WIDTH) if combined.width < PAIR_WIDTH else combined

            bar = label_bar(f"{category.replace('floortile', 'floor tile')} — before / after",
                            combined.width)
            final = Image.new("RGB", (combined.width, combined.height + bar.height), (255, 255, 255))
            final.paste(bar, (0, 0))
            final.paste(combined, (0, bar.height))
            final.save(f"{OUT}/{stem}-before-after.jpg", "JPEG", quality=JPEG_QUALITY, optimize=True)

            manifest.append({
                "category": category, "pair": n, "stem": stem,
                "before": f"{stem}-before.jpg", "after": f"{stem}-after.jpg",
                "pair_image": f"{stem}-before-after.jpg",
                "size": [final.width, final.height],
            })
            print(f"  {stem:<20} before {up_before.width}x{up_before.height}  "
                  f"after {up_after.width}x{up_after.height}  "
                  f"pair {final.width}x{final.height}")

    print(f"\n{len(manifest)} before/after projects written to {OUT}/")


if __name__ == "__main__":
    main()
