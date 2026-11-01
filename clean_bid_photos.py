"""Clean site photos for a bid document.

Phone photos of a dim hallway are not bad photographs, they are badly exposed
photographs: the sensor picks a mid-grey that makes a white wall look beige, the
compression smears detail, and the phone leaves its GPS in the EXIF. This makes
them legible and safe to hand a client, and nothing more - no cropping, no
straightening, no invented content.

The correction keys on the **white reference**, not on the histogram.

An earlier version used ``ImageOps.autocontrast(preserve_tone=True)`` and it
misfired on exactly the two photos that needed help, which is why it is gone:

* It scales on the widest channel range, so a frame that is half black floor
  plastic and half bright wall gets *darkened* overall. Measured on site-2: the
  median fell 78 -> 62 before anything else ran.
* It stretches to 255, so a flat white wall with one scuff mark clipped 2.4% of
  its pixels - destroying the detail the photo exists to show.

Interior photos almost always contain a surface that should read as white: a
wall, a door, a casing. The 97th percentile of luminance is a good proxy for
it, and it is robust to a large dark region occupying the other half of the
frame, because it only looks at the bright end. So every photo is gained so
that p97 lands on 245. That also makes a set shot in one dark hallway look like
one set, instead of each frame being stretched by its own histogram.

Clipping is then bounded by construction rather than by a retry loop: p97 sits
at 245, so only pixels above ~1.04x p97 can reach 255.

Usage::

    venv/bin/python clean_bid_photos.py --src ~/tmp/photos-raw \\
        --out ~/tmp/photos-clean
"""

from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

# Where a surface that should be white lands. 245 rather than 255 leaves the
# highlight end some room, so the unsharp mask does not push it over.
TARGET_WHITE = 245
# Never brighten more than this; past 2x the noise and JPEG blocking come up
# with the exposure.
MAX_GAIN = 2.0
# Shadows are only lifted when the median says the frame is genuinely dark, and
# then gently - a big lift greys out black plastic that should stay black.
LIFT_BELOW_MEDIAN = 100
LIFT_MAX = 0.55
# Only pull contrast when the frame is flat AND there is highlight headroom to
# spend on it. site-5 is a blank wall; its low spread is honest, not a fault.
TARGET_STD = 46.0
MAX_CONTRAST = 1.15
CONTRAST_HEADROOM = 248
MAX_SATURATION = 1.06


def _percentile(hist: list[int], q: float) -> int:
    total = sum(hist)
    if not total:
        return 0
    acc = 0
    for value, count in enumerate(hist):
        acc += count
        if acc >= total * q:
            return value
    return 255


def _stats(im: Image.Image) -> dict:
    lum = im.convert("L")
    hist = lum.histogram()
    stat = ImageStat.Stat(lum)
    return {
        "mean": stat.mean[0],
        "std": stat.stddev[0],
        "p05": _percentile(hist, 0.05),
        "p50": _percentile(hist, 0.50),
        "p97": _percentile(hist, 0.97),
        "p99": _percentile(hist, 0.99),
        "clipped": sum(hist[254:]) / max(sum(hist), 1) * 100.0,
    }


def _gain_lut(gain: float) -> list[int]:
    return [max(0, min(255, int(round(i * gain)))) for i in range(256)]


def _shadow_lift(amount: float) -> list[int]:
    """Curve that raises the dark end and leaves 255 where it is.

    ``amount`` 0 is identity; 1 is a full gamma-0.72 lift weighted by how dark
    the pixel already is, so a highlight is untouched and a shadow moves most.
    """
    lut = []
    for i in range(256):
        x = i / 255.0
        lifted = x ** 0.72
        y = x + (lifted - x) * (1.0 - x) * amount
        lut.append(max(0, min(255, int(round(y * 255)))))
    return lut


def clean_one(src: Path, dst: Path, max_edge: int, quality: int) -> dict:
    im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    before = _stats(im)

    # 1. Exposure: put the white reference where white belongs.
    gain = min(MAX_GAIN, max(1.0, TARGET_WHITE / max(before["p97"], 1)))
    if gain > 1.005:
        im = im.point(_gain_lut(gain) * 3)

    # 2. Shadow lift, only if the median still says the frame is dark.
    mid = _stats(im)
    lift = 0.0
    if mid["p50"] < LIFT_BELOW_MEDIAN:
        lift = min(LIFT_MAX, (LIFT_BELOW_MEDIAN - mid["p50"]) / 70.0 * LIFT_MAX)
        im = im.point(_shadow_lift(lift) * 3)

    # 3. Contrast, only into available headroom.
    post = _stats(im)
    contrast = 1.0
    if post["std"] < TARGET_STD and post["p99"] < CONTRAST_HEADROOM:
        contrast = min(MAX_CONTRAST, TARGET_STD / max(post["std"], 1.0))
        im = ImageEnhance.Contrast(im).enhance(contrast)

    # 4. Indoor light is flat; a little colour back.
    im = ImageEnhance.Color(im).enhance(MAX_SATURATION)
    im = im.filter(ImageFilter.UnsharpMask(radius=1.5, percent=105, threshold=3))
    im.thumbnail((max_edge, max_edge), Image.LANCZOS)

    # Re-encoding with no exif= argument is what strips the metadata; the
    # orientation is already baked into the pixels by exif_transpose.
    im.save(dst, "JPEG", quality=quality, optimize=True, progressive=True)
    return {"before": before, "after": _stats(im), "size": im.size,
            "bytes": dst.stat().st_size, "gain": gain, "lift": lift,
            "contrast": contrast}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, help="folder of original photos")
    ap.add_argument("--out", required=True, help="folder for cleaned photos")
    ap.add_argument("--max-edge", type=int, default=1600)
    ap.add_argument("--quality", type=int, default=90)
    args = ap.parse_args()

    src = Path(args.src).expanduser()
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    exts = {".jpg", ".jpeg", ".png", ".heic"}
    files = sorted((p for p in src.iterdir()
                    if p.is_file() and p.suffix.lower() in exts),
                   key=lambda p: (len(p.stem), p.stem))
    if not files:
        raise SystemExit(f"no images in {src}")

    print(f"{'file':12s} {'median':>12s} {'white p97':>13s} "
          f"{'spread':>12s} {'clip':>11s}  applied")
    worst = 0.0
    for p in files:
        dst = out / (p.stem + ".jpg")
        r = clean_one(p, dst, args.max_edge, args.quality)
        b, a = r["before"], r["after"]
        worst = max(worst, a["clipped"])
        print(f"{p.name:12s} "
              f"{b['p50']:5.0f} -> {a['p50']:4.0f} "
              f"{b['p97']:5.0f} -> {a['p97']:4.0f} "
              f"{b['std']:4.0f} -> {a['std']:4.0f} "
              f"{b['clipped']:4.1f} -> {a['clipped']:4.1f}%  "
              f"gain x{r['gain']:.2f}"
              + (f", lift {r['lift']:.2f}" if r["lift"] else "")
              + (f", contrast x{r['contrast']:.2f}" if r["contrast"] > 1.005 else ""))
    print(f"\n{len(files)} photo(s) -> {out}")
    if worst > 1.0:
        print(f"WARNING: {worst:.1f}% of pixels clipped in the worst frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
