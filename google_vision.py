"""Google Cloud Vision integration for BizStack.

Label detection, object localization, and OCR on a customer-supplied photo, so a
photo can become a scope and a ballpark before anyone drives out.

Fails soft on purpose: every entry point returns None when the API is
unconfigured, disabled, throttled, or errors, so a customer upload falls back to
the normal manual quote path instead of 500ing the request.
"""

import base64
import hashlib
import io
import json
import os
import urllib.error
import urllib.request

ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"

MAX_EDGE = 1600
MAX_ENCODED_BYTES = 3_500_000
JPEG_START_QUALITY = 82
JPEG_MIN_QUALITY = 35

FEATURES = (
    {"type": "LABEL_DETECTION", "maxResults": 25},
    {"type": "OBJECT_LOCALIZATION", "maxResults": 25},
    {"type": "TEXT_DETECTION", "maxResults": 40},
)


def api_key() -> str:
    """Prefer a dedicated Vision key; fall back to the Maps key.

    The Maps key is restricted per-API, so this only works once Vision is added
    to that key's API restrictions. A dedicated key keeps the blast radius of
    either credential smaller.
    """
    return (
        (os.getenv("GOOGLE_VISION_API_KEY", "") or "").strip()
        or (os.getenv("GOOGLE_MAPS_API_KEY", "") or "").strip()
    )


def is_configured() -> bool:
    return bool(api_key())


def _pil_image():
    try:
        from PIL import Image

        return Image
    except Exception:
        return None


def _lanczos(Image):
    """Pillow 10 moved the resampling filters onto Image.Resampling."""
    resampling = getattr(Image, "Resampling", None)
    if resampling is not None and hasattr(resampling, "LANCZOS"):
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS", None)


def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Downscale and re-encode so the annotate request fits Vision's body limit.

    A modern phone photo base64-encodes well past the JSON payload cap, which
    the API rejects outright. Label and object detection do not need full
    resolution, so the long edge is capped and quality steps down until the
    encoded size fits. Non-image or undecodable bytes are returned untouched so
    the caller decides what to do with them.
    """
    Image = _pil_image()
    if Image is None or not data:
        return data, "image/jpeg"
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return data, "image/jpeg"
    if max(img.size) > MAX_EDGE:
        scale = MAX_EDGE / float(max(img.size))
        img = img.resize(
            (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
            _lanczos(Image),
        )
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    quality = JPEG_START_QUALITY
    encoded = b""
    while quality >= JPEG_MIN_QUALITY:
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        encoded = out.getvalue()
        if len(encoded) <= MAX_ENCODED_BYTES:
            break
        quality -= 10
    return encoded, "image/jpeg"


def image_fingerprint(data: bytes) -> str:
    """Stable id for a photo, so re-uploads can reuse a cached result.

    Computed on the *original* bytes, so it is unaffected by re-encoding.
    """
    return hashlib.sha256(data or b"").hexdigest()


def _normalized_box(poly: dict | None) -> dict | None:
    verts = ((poly or {}).get("normalizedVertices")) or []
    if len(verts) < 4:
        return None
    xs = [float(v.get("x") or 0) for v in verts]
    ys = [float(v.get("y") or 0) for v in verts]
    return {
        "left": round(min(xs), 4),
        "top": round(min(ys), 4),
        "width": round(max(xs) - min(xs), 4),
        "height": round(max(ys) - min(ys), 4),
    }


def _parse(first: dict) -> dict:
    objects = first.get("localizedObjectAnnotations") or first.get("objectAnnotations") or []
    text_lines = [
        (t.get("description") or "").strip()
        for t in (first.get("textAnnotations") or [])
        if (t.get("description") or "").strip()
    ]
    labels = []
    for item in first.get("labelAnnotations") or []:
        description = (item.get("description") or "").strip()
        if not description:
            continue
        labels.append({"description": description, "score": round(float(item.get("score") or 0), 4)})
    parsed_objects = []
    for item in objects:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        parsed_objects.append(
            {
                "name": name,
                "score": round(float(item.get("score") or 0), 4),
                "box": _normalized_box(item.get("boundingPoly")),
            }
        )
    return {
        "labels": labels,
        "objects": parsed_objects,
        "text": " ".join(text_lines).strip(),
    }


def annotate(data: bytes) -> dict | None:
    """Run label detection, object localization, and OCR in one call.

    Returns {"labels", "objects", "text"} or None if Vision is unavailable for
    any reason. The three features are billed as three units per image, so
    callers should cache on image_fingerprint rather than re-analyze.
    """
    key = api_key()
    if not key or not data:
        return None
    prepared, _mime = prepare_image(data)
    payload = {
        "requests": [
            {
                "image": {"content": base64.b64encode(prepared).decode()},
                "features": [dict(f) for f in FEATURES],
                "imageContext": {"languageHints": ["en"]},
            }
        ]
    }
    request = urllib.request.Request(
        ANNOTATE_URL,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:300].decode("utf-8", "replace")
        except Exception:
            pass
        print(f"[vision] annotate HTTP {exc.code}: {detail}", flush=True)
        return None
    except Exception as exc:
        print(f"[vision] annotate failed: {exc}", flush=True)
        return None
    responses = body.get("responses") or []
    if not responses:
        print("[vision] annotate returned no responses", flush=True)
        return None
    first = responses[0] or {}
    if first.get("error"):
        print(f"[vision] annotate error: {first.get('error')}", flush=True)
        return None
    return _parse(first)
