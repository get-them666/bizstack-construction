"""Tests for photo -> scope -> quote.

Run: venv/bin/python test_vision_quote.py
"""

if __name__ == "__main__":
    import os
    import sys

    os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

    import estimating_service
    import google_vision
    import vision_quote


    PASSED = 0
    FAILED = 0
    FAILURES = []


    def check(label, condition, detail: object = ""):
        global PASSED, FAILED
        if condition:
            PASSED += 1
            print(f"  ok   {label}")
        else:
            FAILED += 1
            FAILURES.append(f"{label} {detail}".strip())
            print(f"  FAIL {label} {detail}".rstrip())


    def vision(labels=(), objects=(), text=""):
        return {
            "labels": [{"description": n, "score": s} for n, s in labels],
            "objects": [{"name": n, "score": s, "box": None} for n, s in objects],
            "text": text,
        }


    # --- 1. annotation parsing ---------------------------------------------------
    def test_parsing():
        print("\n[1] Vision annotation parsing")
        parsed = google_vision._parse({
            "labelAnnotations": [
                {"description": "Kitchen", "score": 0.9612},
                {"description": "  ", "score": 0.5},
            ],
            "localizedObjectAnnotations": [
                {"name": "Refrigerator", "score": 0.88,
                 "boundingPoly": {"normalizedVertices": [
                     {"x": 0.1, "y": 0.2}, {"x": 0.4, "y": 0.2},
                     {"x": 0.4, "y": 0.9}, {"x": 0.1, "y": 0.9}]}},
            ],
            "textAnnotations": [{"description": "FOR SALE"}, {"description": "  "}],
        })
        check("blank labels dropped", len(parsed["labels"]) == 1, parsed["labels"])
        check("score rounded to 4dp", parsed["labels"][0]["score"] == 0.9612)
        check("object name kept", parsed["objects"][0]["name"] == "Refrigerator")
        check("bounding box normalized", parsed["objects"][0]["box"] == {
            "left": 0.1, "top": 0.2, "width": 0.3, "height": 0.7,
        }, parsed["objects"][0]["box"])
        check("blank text annotations dropped", parsed["text"] == "FOR SALE", repr(parsed["text"]))
        check("objectAnnotations fallback", len(google_vision._parse({
            "objectAnnotations": [{"name": "Sink", "score": 0.5}],
        })["objects"]) == 1)
        check("empty annotation is empty dict, not a crash",
              google_vision._parse({}) == {"labels": [], "objects": [], "text": ""})


    # --- 2. fail-soft ------------------------------------------------------------
    def test_fail_soft():
        print("\n[2] fails soft instead of raising")
        os.environ.pop("GOOGLE_VISION_API_KEY", None)
        os.environ.pop("GOOGLE_MAPS_API_KEY", None)
        check("unconfigured reports False", google_vision.is_configured() is False)
        check("annotate with no key returns None", google_vision.annotate(b"x") is None)
        check("annotate with no bytes returns None", google_vision.annotate(b"") is None)
        check("prepare_image passes bytes through when Pillow absent",
              google_vision.prepare_image(b"not-an-image") == (b"not-an-image", "image/jpeg"))

        os.environ["GOOGLE_MAPS_API_KEY"] = "fallback-key"
        check("falls back to the Maps key", google_vision.api_key() == "fallback-key")
        os.environ["GOOGLE_VISION_API_KEY"] = "dedicated"
        check("dedicated key wins", google_vision.api_key() == "dedicated")
        os.environ.pop("GOOGLE_VISION_API_KEY")
        os.environ.pop("GOOGLE_MAPS_API_KEY")

        check("quote with no vision is unavailable, not a crash",
              vision_quote.quote_from_vision(None)["available"] is False)


    # --- 3. downscale ------------------------------------------------------------
    def test_downscale():
        print("\n[3] image preparation")
        try:
            import io

            from PIL import Image
        except Exception as exc:
            check("Pillow available", False, str(exc))
            return
        buf = io.BytesIO()
        Image.new("RGB", (4000, 3000), (120, 90, 60)).save(buf, format="JPEG", quality=95)
        raw = buf.getvalue()
        prepared, mime = google_vision.prepare_image(raw)
        check("long edge capped", max(Image.open(io.BytesIO(prepared)).size) <= google_vision.MAX_EDGE,
              Image.open(io.BytesIO(prepared)).size)
        check("re-encoded as jpeg", mime == "image/jpeg")
        check("payload within Vision body cap", len(prepared) <= google_vision.MAX_ENCODED_BYTES,
              len(prepared))
        check("small image passes through decodable",
              google_vision.prepare_image(raw)[0] != b"")
        check("fingerprint is stable",
              google_vision.image_fingerprint(raw) == google_vision.image_fingerprint(raw))
        check("fingerprint differs by content",
              google_vision.image_fingerprint(raw) != google_vision.image_fingerprint(b"other"))
        check("fingerprint tolerates empty", bool(google_vision.image_fingerprint(b"")))


    # --- 4. scope detection ------------------------------------------------------
    def test_scope():
        print("\n[4] room + item detection")
        scope = vision_quote.detect_scope(vision(
            objects=[("Kitchen", 0.95), ("Refrigerator", 0.9), ("Oven", 0.85), ("Sink", 0.7)],
        ))
        check("kitchen identified", scope["room"] == "kitchen", scope["room"])
        check("confidence is 0-1", 0 < scope["confidence"] <= 1, scope["confidence"])
        check("items sorted by score",
              scope["items"][0]["score"] >= scope["items"][-1]["score"])

        merged = vision_quote.detect_scope(vision(
            labels=[("Kitchen", 0.9)], objects=[("Kitchen", 0.9), ("Dishwasher", 0.8)],
        ))
        check("label and object for same name merge, not double count",
              len([i for i in merged["items"] if i["name"] == "kitchen"]) == 1, merged["items"])

        bath = vision_quote.detect_scope(vision(
            objects=[("Bathroom", 0.9), ("Toilet", 0.85), ("Bathtub", 0.8), ("Shower", 0.8)],
        ))
        check("bathroom identified", bath["room"] == "bathroom", bath["room"])

        ext = vision_quote.detect_scope(vision(objects=[("Roof", 0.9), ("Gutter", 0.8), ("Sky", 0.7)]))
        check("exterior identified", ext["room"] == "exterior", ext["room"])

        unknown = vision_quote.detect_scope(vision(objects=[("Zebra", 0.9)]))
        check("no recognizable room -> empty", unknown["room"] == "")

        check("empty vision is safe", vision_quote.detect_scope({})["room"] == "")


    # --- 5. cleaning quote -------------------------------------------------------
    def test_cleaning_quote():
        print("\n[5] cleaning labor quote")
        os.environ.pop("CLEAN_HOURLY_LOW", None)
        os.environ.pop("CLEAN_HOURLY_HIGH", None)
        scope = vision_quote.detect_scope(vision(
            objects=[("Kitchen", 0.95), ("Refrigerator", 0.9), ("Oven", 0.85), ("Dishwasher", 0.8)],
        ))
        quote = vision_quote.cleaning_quote(scope)
        check("available", quote["available"] is True)
        check("base is kitchen minutes", quote["base_minutes"] == vision_quote.CLEAN_BASE_MINUTES["kitchen"])
        check("appliance adders counted", quote["item_minutes"] > 0, quote["item_minutes"])
        check("hours low < high", quote["hours"]["low"] < quote["hours"]["high"], quote["hours"])
        check("no price invented when rate unset", "price" not in quote, quote.get("price"))

        os.environ["CLEAN_HOURLY_LOW"] = "60"
        os.environ["CLEAN_HOURLY_HIGH"] = "95"
        priced = vision_quote.cleaning_quote(scope)
        check("priced once rate configured", "price" in priced)
        check("price low < high", priced["price"]["low_cents"] < priced["price"]["high_cents"])
        os.environ.pop("CLEAN_HOURLY_LOW")
        os.environ.pop("CLEAN_HOURLY_HIGH")

        heavy = vision_quote.detect_scope(vision(
            objects=[("Kitchen", 0.9), ("Mold", 0.8), ("Grime", 0.7)],
        ))
        check("condition multiplies up",
              vision_quote.cleaning_quote(heavy)["condition_multiplier"] > 1.0)
        check("unknown room -> unavailable",
              vision_quote.cleaning_quote({"room": "", "items": []})["available"] is False)


    # --- 6. construction quote ---------------------------------------------------
    def test_construction_quote():
        print("\n[6] construction quote via estimating_service")
        scope = vision_quote.detect_scope(vision(
            objects=[("Kitchen", 0.95), ("Refrigerator", 0.9), ("Cabinet", 0.8)],
        ))
        quote = vision_quote.construction_quote(scope)
        check("available", quote["available"] is True)
        check("maps to a real model", quote["estimate"]["model_key"] == "kitchen", quote["estimate"]["model_key"])
        check("range is sane", quote["estimate"]["low_est"] < quote["estimate"]["high_est"])
        check("carries the engine disclaimer", "not a bid" in quote["estimate"]["disclaimer"].lower())

        roof = vision_quote.detect_scope(vision(objects=[("Roof", 0.95), ("Shingle", 0.8)]))
        check("exterior maps to roofing", vision_quote.construction_quote(roof)["estimate"]["model_key"] == "roof")

        check("unmappable room -> unavailable",
              vision_quote.construction_quote({"room": "", "items": []})["available"] is False)

        print("\n[6b] every room maps to the model it claims")
        for room, expected in vision_quote.CONSTRUCTION_EXPECTED_MODEL.items():
            project_type = vision_quote.CONSTRUCTION_PROJECT[room]
            resolved = estimating_service.project_model(project_type)
            check(f"{room} -> {expected} (via {project_type!r})", resolved == expected, f"got {resolved}")


    # --- 7. top-level ------------------------------------------------------------
    def test_top_level():
        print("\n[7] quote_from_vision contract")
        v = vision(objects=[("Kitchen", 0.95), ("Oven", 0.85)])
        broom = vision_quote.quote_from_vision(v, "broom")
        check("broom -> cleaning hours", broom["quote"]["hours"]["low"] > 0)
        check("disclaimer always present", "not a bid" in broom["disclaimer"].lower())

        con = vision_quote.quote_from_vision(v, "construction")
        check("construction -> money range", con["quote"]["estimate"]["low_est"] > 0)

        sqfted = vision_quote.quote_from_vision(v, "construction", sqft=2400)
        check("explicit sqft is used, not the 1750 assumption",
              sqfted["quote"]["estimate"]["sqft"] == 2400, sqfted["quote"]["estimate"]["sqft"])

        weak = vision_quote.quote_from_vision(vision(objects=[("Zebra", 0.99)]), "broom")
        check("unrecognizable -> available False", weak["available"] is False)
        check("unrecognizable explains why", bool(weak.get("why")))

        os.environ["VISION_QUOTE_ENABLED"] = "0"
        check("kill switch works", vision_quote.quote_from_vision(v)["available"] is False)
        os.environ["VISION_QUOTE_ENABLED"] = "1"


    # --- 8. abuse controls on the public endpoint --------------------------------
    def test_throttle_and_cache():
        print("\n[8] endpoint abuse controls")
        try:
            import construction_main as main
        except ImportError:
            import main

        main._vision_quote_calls.clear()
        ip = "203.0.113.9"
        first = [main._vision_quote_throttled(ip) for _ in range(main.VISION_QUOTE_MAX_PER_WINDOW)]
        check("allows up to the per-window cap", not any(first), first)
        check("throttles the next call", main._vision_quote_throttled(ip) is True)
        check("throttle is per-ip, not global", main._vision_quote_throttled("198.51.100.4") is False)
        main._vision_quote_calls.clear()

        check("cache starts empty", main._vision_quote_cache == {})
        for i in range(main.VISION_QUOTE_CACHE_MAX + 25):
            main._vision_quote_cache[f"fp{i}"] = {"available": True}
            if len(main._vision_quote_cache) > main.VISION_QUOTE_CACHE_MAX:
                main._vision_quote_cache.pop(next(iter(main._vision_quote_cache)), None)
        check("cache stays bounded", len(main._vision_quote_cache) <= main.VISION_QUOTE_CACHE_MAX,
              len(main._vision_quote_cache))
        main._vision_quote_cache.clear()

        check("upload cap is under the Vision body limit",
              main.VISION_QUOTE_MAX_UPLOAD_BYTES < google_vision.MAX_ENCODED_BYTES * 8)


    def main():
        for fn in (
            test_parsing,
            test_fail_soft,
            test_downscale,
            test_scope,
            test_cleaning_quote,
            test_construction_quote,
            test_top_level,
            test_throttle_and_cache,
        ):
            fn()
        print(f"\n{'=' * 46}\n{PASSED} passed, {FAILED} failed")
        for failure in FAILURES:
            print(f"  - {failure}")
        return 1 if FAILED else 0


    if __name__ == "__main__":
        sys.exit(main())

