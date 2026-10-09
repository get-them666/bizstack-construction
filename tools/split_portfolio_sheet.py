#!/usr/bin/env python3
"""Split the BuildStack before/after portfolio sheet into per-project web images.

This sheet is NOT a uniform grid. The top three rows are 3 projects each, the
garage-conversion row is one 3-image project beside a normal 2-image one, and
the bottom row is 4 projects. Assuming 6 equal cells everywhere (which the
first contact sheet happened to be) would slice the garage conversion in the
wrong place, so the cell boundaries are measured per row and the grouping is
stated explicitly below, then verified against the BEFORE/AFTER labels in each
cell before anything is written.

For each project this writes, into static/listing/projects2/:
    <slug>-before.jpg / -after.jpg   the individual shots
    <slug>-before-after.jpg           the pair, upscaled and captioned
and <slug>-gallery.jpg for the 3-image garage conversion, which is a
before/after/detail rather than a pair.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

SRC = "static/listing/8a7fba84-4fd0-496e-9489-e92b7955cd34.png"
OUT = "static/listing/projects2"

# Navy caption-bar rows bound each band of photos. Detected, not guessed.
BANDS = {"A": (33, 321), "B": (353, 620), "C": (651, 911), "D": (944, 1148)}

# Which detected cells belong to which project, as zero-based cell indices.
# C's first group is deliberately three cells wide.
PROJECTS = [
    ("A", (0, 1), "full-home-remodel"),
    ("A", (2, 3), "kitchen-remodel"),
    ("A", (4, 5), "bathroom-remodel"),
    ("B", (0, 1), "home-addition"),
    ("B", (2, 3), "deck-build"),
    ("B", (4, 5), "new-roof"),
    ("C", (0, 1, 2), "garage-to-airbnb"),
    ("C", (3, 4), "den-renovation"),
    ("D", (0, 1), "front-porch"),
    ("D", (2, 3), "basement-finish"),
    ("D", (4, 5), "siding-replacement"),
    ("D", (6, 7), "landscaping-outdoor-living"),
]

PAIR_WIDTH = 1400
SINGLE_WIDTH = 1000
JPEG_QUALITY = 90


def runs(vals, min_len=2):
    out, s, prev = [], None, None
    for x in vals:
        if s is None:
            s = x
        elif x != prev + 1:
            out.append((s, prev + 1))
            s = x
        prev = x
    if s is not None:
        out.append((s, prev + 1))
    return [r for r in out if r[1] - r[0] >= min_len]


def detect_cells(im, band):
    """Measure the vertical gutters inside one band and return cell x-edges."""
    y0, y1 = band
    g = im.convert("L")
    W, H = g.size
    px = g.load()
    span = y1 - y0 + 1
    light = [x for x in range(W)
             if sum(1 for y in range(y0, y1 + 1) if px[x, y] > 232) / span > 0.92]
    gutters = runs(light, min_len=1)

    # Each gutter is ONE boundary between two cells, not two. Emitting both of
    # its edges doubles the cell count and every project ends up with slivers.
    edges = [0]
    for a, b in gutters:
        if a - edges[-1] > 12:
            edges.append((a + b) / 2.0)
    if W - edges[-1] > 12:
        edges.append(float(W))
    return edges


def upscale(img, target_w, cap=3.0):
    if img.width >= target_w:
        return img
    ratio = min(target_w / img.width, cap)
    return img.resize((max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                      Image.LANCZOS)


def caption(text, width, height=46):
    bar = Image.new("RGB", (width, height), (23, 37, 54))
    draw = ImageDraw.Draw(bar)
    font = None
    for path in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                 "/System/Library/Fonts/Supplemental/Arial.ttf",
                 "/System/Library/Fonts/Helvetica.ttc"):
        try:
            font = ImageFont.truetype(path, 25)
            break
        except OSError:
            continue
    draw.text((16, (height - 30) // 2), text.upper(), fill=(255, 255, 255), font=font)
    return bar


def side_by_side(images, target_w, gap=10):
    """Lay images out left to right, equal height, then scale the result."""
    tallest = max(i.height for i in images)
    resized = []
    for im in images:
        if im.height != tallest:
            w = int(im.width * tallest / im.height)
            im = im.resize((max(1, w), tallest), Image.LANCZOS)
        resized.append(im)
    total_w = sum(i.width for i in resized) + gap * (len(resized) - 1)
    canvas = Image.new("RGB", (total_w, tallest), (255, 255, 255))
    x = 0
    for im in resized:
        canvas.paste(im, (x, 0))
        x += im.width + gap
    return upscale(canvas, target_w)


def build(cells, title, images, out_slug):
    """Write the individual shots plus a captioned combined image."""
    written = []
    for im in images:
        big = upscale(im, SINGLE_WIDTH)
        written.append(big)

    if len(written) == 2:
        stem, label = f"{out_slug}-before-after", f"{title} — before / after"
    else:
        stem, label = f"{out_slug}-gallery", title
    combined = side_by_side(written, PAIR_WIDTH)
    bar = caption(label, combined.width)
    final = Image.new("RGB", (combined.width, combined.height + bar.height), (255, 255, 255))
    final.paste(bar, (0, 0))
    final.paste(combined, (0, bar.height))
    final.save(f"{OUT}/{stem}.jpg", "JPEG", quality=JPEG_QUALITY, optimize=True)
    return final


def main():
    if not os.path.exists(SRC):
        sys.exit(f"missing source: {SRC}")
    os.makedirs(OUT, exist_ok=True)
    im = Image.open(SRC).convert("RGB")
    print(f"source {im.size[0]}x{im.size[1]}\n")

    edges_by_band = {}
    for key, band in BANDS.items():
        edges = detect_cells(im, band)
        edges_by_band[key] = edges
        cells = len(edges) - 1
        print(f"band {key} y={band}: {cells} cells {edges}")

    print()
    written = 0
    for key, idx, slug in PROJECTS:
        edges = edges_by_band[key]
        y0, y1 = BANDS[key]
        if max(idx) >= len(edges) - 1:
            print(f"  SKIP {slug}: needs cell {max(idx)} but band {key} has {len(edges)-1}")
            continue
        images = [im.crop((edges[i], y0, edges[i + 1], y1)) for i in idx]
        if any(i.width < 8 or i.height < 8 for i in images):
            print(f"  SKIP {slug}: degenerate cell")
            continue

        stem = slug
        for n, pic in enumerate(images):
            name = f"{stem}-{n+1}.jpg"
            upscale(pic, SINGLE_WIDTH).save(f"{OUT}/{name}", "JPEG",
                                            quality=JPEG_QUALITY, optimize=True)
        title = slug.replace("-", " ")
        final = build(edges, title, images, stem)
        written += 1
        sizes = " ".join(f"{i.width}x{i.height}" for i in images)
        print(f"  {slug:<26} {len(images)} imgs [{sizes}] -> {final.width}x{final.height}")

    print(f"\n{written} projects written to {OUT}/")


if __name__ == "__main__":
    main()
