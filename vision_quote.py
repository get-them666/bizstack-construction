"""Turn a customer photo into a scope and a ballpark.

Cloud Vision tells us what is in the frame, not how big it is. That shapes this
module: the cleaning side prices by task list (a bathroom is roughly the same
job whatever its dimensions), and the construction side leans on the fixed-price
models in estimating_service, falling back to its assumed-sqft path only when
the room genuinely needs square footage.

Every rate below is a placeholder for the owner to retune via env, following the
EST_* convention already used by estimating_service. Nothing here is a bid.
"""

import os

import estimating_service

VISION_QUOTE_ENABLED_ENV = "VISION_QUOTE_ENABLED"
DEFAULT_MIN_CONFIDENCE = 0.35


def _env_float(key, default):
    try:
        return float(os.getenv(key, "") or default)
    except (TypeError, ValueError):
        return float(default)


def is_enabled() -> bool:
    return (os.getenv(VISION_QUOTE_ENABLED_ENV, "1") or "1").strip().lower() in ("1", "true", "yes", "on")


ROOM_CUES = {
    "kitchen": (
        "kitchen", "refrigerator", "oven", "microwave", "dishwasher", "stove",
        "coffeemaker", "toaster", "blender", "cabinet", "cabinetry", "counter top",
        "countertop", "kitchen island", "sink", "bottle", "dish", "cereal",
    ),
    "bathroom": (
        "bathroom", "toilet", "bathtub", "shower", "towel", "mirror", "sink",
        "shower door", "toilet paper", "vanity", "bathroom cabinet", "wash basin",
    ),
    "bedroom": ("bedroom", "bed", "nightstand", "dresser", "pillow", "blanket", "wardrobe"),
    "living_room": (
        "living room", "sofa", "couch", "television", "coffee table", "armchair",
        "bookcase", "ottoman",
    ),
    "dining_room": ("dining room", "dining table", "chair", "china cabinet"),
    "office": ("office", "desk", "computer", "bookcase", "whiteboard"),
    "garage": ("garage", "workbench", "tool", "shelf", "ladder"),
    "basement": ("basement", "laundry", "washer", "dryer", "furnace"),
    "exterior": (
        "exterior", "roof", "gutter", "siding", "brick", "concrete", "driveway",
        "fence", "deck", "landscaping", "tree", "lawn", "house", "building",
        "sky", "chimney",
    ),
}

CONDITION_CUES = {
    "heavy soil": 1.35,
    "stained": 1.25,
    "mold": 1.4,
    "mildew": 1.4,
    "clutter": 1.15,
    "pet": 1.2,
    "grime": 1.25,
    "rust": 1.2,
    "water damage": 1.45,
    "light": 0.85,
    "bright": 0.9,
}

CONSTRUCTION_PROJECT = {
    "kitchen": "Kitchen remodel",
    "bathroom": "Bathroom remodel",
    "exterior": "Roofing & Siding",
    "basement": "Basement",
    "garage": "Garage / ADU",
    "living_room": "Interior Refresh",
    "bedroom": "Interior Refresh",
    "dining_room": "Interior Refresh",
    "office": "Interior Refresh",
}

CONSTRUCTION_EXPECTED_MODEL = {
    "kitchen": "kitchen",
    "bathroom": "bath",
    "exterior": "roof",
    "basement": "basement",
    "garage": "whole-home",
    "living_room": "refresh",
    "bedroom": "refresh",
    "dining_room": "refresh",
    "office": "refresh",
}

DISCLAIMER = (
    "Generated from a single photo using automated image analysis. It is a "
    "planning aid, not a bid. Final pricing comes from a free on-site walkthrough "
    "and a written, fixed-price scope."
)


def _cues_for(observations):
    rooms = {}
    for room, cues in ROOM_CUES.items():
        matched = {obs["name"]: obs["score"] for obs in observations if obs["name"] in cues}
        if matched:
            rooms[room] = matched
    return rooms


def detect_scope(vision: dict) -> dict:
    """Reduce a Vision annotation to rooms, an item inventory, and confidence.

    Labels and objects are merged so a kitchen seen as both a scene label and a
    localized object is not counted twice for the same room.
    """
    merged = {}
    for item in (vision or {}).get("objects") or []:
        name = (item.get("name") or "").strip().lower()
        if name:
            merged[name] = max(merged.get(name, 0.0), float(item.get("score") or 0))
    for item in (vision or {}).get("labels") or []:
        name = (item.get("description") or "").strip().lower()
        if name:
            merged[name] = max(merged.get(name, 0.0), float(item.get("score") or 0))
    observations = [{"name": name, "score": round(score, 4)} for name, score in merged.items()]

    rooms = _cues_for(observations)
    ranked = sorted(
        rooms.items(),
        key=lambda kv: sum(kv[1].values()),
        reverse=True,
    )
    total = sum(sum(v.values()) for v in rooms.values()) or 0.0
    top_room = ranked[0][0] if ranked else ""
    top_score = sum(ranked[0][1].values()) if ranked else 0.0
    confidence = round(top_score / total, 3) if total else 0.0

    condition = []
    for name, score in merged.items():
        for cue, multiplier in CONDITION_CUES.items():
            if cue in name:
                condition.append({"cue": cue, "score": round(score, 4), "multiplier": multiplier})
    condition.sort(key=lambda c: c["score"], reverse=True)

    return {
        "room": top_room,
        "confidence": confidence,
        "rooms": [{"room": r, "score": round(sum(v.values()), 4)} for r, v in ranked],
        "items": sorted(observations, key=lambda o: o["score"], reverse=True)[:20],
        "condition": condition[:5],
        "text": ((vision or {}).get("text") or "")[:500],
    }



def construction_quote(scope: dict) -> dict:
    """Ballpark for the detected room via the existing estimating engine."""
    room = scope.get("room") or ""
    project_type = CONSTRUCTION_PROJECT.get(room)
    if not project_type:
        return {"available": False, "why": "no mappable project type for this photo"}
    expected = CONSTRUCTION_EXPECTED_MODEL.get(room)
    if expected and estimating_service.project_model(project_type) != expected:
        print(
            f"[vision-quote] {project_type!r} resolves to "
            f"{estimating_service.project_model(project_type)!r}, expected {expected!r}; "
            "check PROJECT_MODEL_MAP spelling",
            flush=True,
        )
    estimate = estimating_service.auto_quote(project_type)
    if not estimate:
        return {"available": False, "why": "no estimate for this project type"}
    return {
        "available": True,
        "room": room,
        "project_type": project_type,
        "estimate": estimate,
    }


def quote_from_vision(vision: dict | None, sqft: float | None = None) -> dict:
    """Full photo -> scope -> quote. Returns an `available: False` shape when
    Vision is unconfigured, the API failed, or nothing recognizable was found,
    so the caller can always fall back to the normal manual quote path."""
    if not is_enabled():
        return {"available": False, "why": "photo quoting disabled"}
    if not vision:
        return {"available": False, "why": "image analysis unavailable"}
    scope = detect_scope(vision)
    threshold = _env_float("VQ_MIN_CONFIDENCE", DEFAULT_MIN_CONFIDENCE)
    if not scope["room"] or scope["confidence"] < threshold:
        return {
            "available": False,
            "why": "could not identify the room with enough confidence",
            "scope": scope,
        }
    quote = construction_quote(scope)
    if sqft and quote.get("available"):
        quote = {
            "available": True,
            "room": scope["room"],
            "project_type": quote["project_type"],
            "estimate": estimating_service.estimate(quote["project_type"], None, float(sqft)) or quote["estimate"],
        }
    return {
        "available": bool(quote.get("available")),
        "company": "construction",
        "scope": scope,
        "quote": quote,
        "disclaimer": DISCLAIMER,
    }
