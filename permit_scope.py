"""Turn raw permit records into plain-English job scope the owner can act on.

A permit row tells you an address and a permit type. It does not tell you
whether the work is a $40k gut reno or a $2k mailbox, nor whether a contractor is
already attached. This module reads each row's scope text and returns a short
summary, a trade category, and how much can be trusted in that read.

It uses OpenAI when OPENAI_API_KEY is set, and falls back to a deterministic
keyword pass otherwise, so the pipeline never stalls on a missing or failing key.
"""
import json
import os
import re

MODEL = os.getenv("PERMIT_SCOPE_MODEL", "gpt-4o-mini")

CATEGORIES = (
    "kitchen", "bath", "deck", "roof", "siding", "windows", "door", "fence",
    "patio", "porch", "garage", "addu", "remodel", "new_construction",
    "pool", "hvac", "electrical", "plumbing", "foundation", "concrete",
    "landscape", "demolition", "solar", "commercial", "other",
)

# Used when the model is unavailable, and as a sanity check on its output.
KEYWORD_CATEGORY = [
    (r"\bdeck|porch|patio|pergola|railing", "deck"),
    (r"\broof|shingle|tear.?off|gaf|ridge", "roof"),
    (r"\bkitchen|cabinet|counter", "kitchen"),
    (r"\bbath|shower|tub|vanity|toilet", "bath"),
    (r"\bsiding|vinyl|stucco|trim|exterior paint", "siding"),
    (r"\bwindow|door\b|glaz", "windows"),
    (r"\bfence|gate\b|railing", "fence"),
    (r"\bgarage|carport", "garage"),
    (r"\bhvac|furnace|ac\b|heat pump|duct|mini.?split", "hvac"),
    (r"\bpool|spa\b", "pool"),
    (r"\bsolar|pv\b|photovoltaic", "solar"),
    (r"\baddition|addu|add on|extend", "addu"),
    (r"\bdemolition|demolish|teardown|raze", "demolition"),
    (r"\bnew (single family|residence|home|construction)|new sfr|new construction", "new_construction"),
    (r"\bfoundation|footing|slab|retaining", "foundation"),
    (r"\bconcrete|driveway|walkway|patio pour", "concrete"),
    (r"\belectrical|panel|receptacle|service|rewire|circuit", "electrical"),
    (r"\bplumb|water heater|drain|sewer|gas line", "plumbing"),
    (r"\bremodel|renovat|gut|interior", "remodel"),
    (r"\bcommercial|tenant|store|office|warehouse", "commercial"),
    (r"\blandscap|grading|site work|clearing", "landscape"),
]

# Small jobs are not worth a sales call; big ones are the pipeline.
SIZE_HINTS = [
    (r"\bnew (single family|residence|construction|dwelling)|duplex|townhous|apartment", "large"),
    (r"\baddition|addu|garage|sunroom|porch|deck\b", "medium"),
    (r"\bpanel|receptacle|circuit|faucet|toilet|valve|outlet|switch", "small"),
    (r"\bduct|water heater|heat pump|furnace|condenser|service", "medium"),
]

_NOISE_RE = re.compile(r"<[^>]+>|[^a-z0-9 ,.'\-()/]+", re.I)


def clean(text: str, limit: int = 400) -> str:
    """Strip markup/junk the feeds leave in description fields."""
    s = _NOISE_RE.sub(" ", str(text or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def keyword_category(text: str) -> str:
    t = (text or "").lower()
    for pattern, cat in KEYWORD_CATEGORY:
        if re.search(pattern, t, re.I):
            return cat
    return "other"


def keyword_size(text: str) -> str:
    t = (text or "").lower()
    for pattern, size in SIZE_HINTS:
        if re.search(pattern, t, re.I):
            return size
    return "unknown"


def build_prompt(rows: list) -> str:
    lines = []
    for r in rows:
        lines.append(
            f"- id={r.get('id')} | type={r.get('work_type') or '?'} | "
            f"scope={clean(r.get('job_description'), 220) or '(none published)'}"
        )
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You help a licensed Virginia general contractor triage residential building "
    "permits. For each permit you are given a permit type and, usually, a short "
    "free-text scope the applicant typed in.\n"
    "For every id, return a JSON object with:\n"
    "  summary: one plain sentence (<=110 chars) a contractor could say out loud, "
    "e.g. 'Rebuild rear deck and replace roof covering'. Do not invent details that "
    "are not implied by the text. If the scope is vague, say what is vague.\n"
    "  category: exactly one of: " + ", ".join(CATEGORIES) + "\n"
    "  size: one of small, medium, large, unknown\n"
    "  confidence: high if the scope text is specific, medium if partially "
    "implied, low if you are guessing.\n"
    "  residential: true or false -- false for clearly commercial work.\n"
    "Return ONLY a JSON array, no prose, no markdown fence. One object per input id, "
    "in the same order."
)


def _extract_json(text: str) -> list:
    """Pull the JSON array out of a model reply that may be fenced or chatty."""
    if not text:
        return []
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    return data if isinstance(data, list) else []


def analyse_rows(rows: list) -> list:
    """Return {id: {summary, category, size, confidence, residential, source}}.

    Falls back to keyword classification per row when the model is unavailable or
    returns unusable output, so a batch never comes back empty.
    """
    rows = [r for r in rows if r.get("id")]
    results: dict = {}

    def fallback(r, reason):
        text = f"{r.get('work_type') or ''} {r.get('job_description') or ''}"
        cat = keyword_category(text)
        desc = clean(r.get("job_description"), 90)
        summary = desc if desc else (f"{r.get('work_type') or 'Permit'} — scope not published")
        return {
            "summary": summary,
            "category": cat,
            "size": keyword_size(text),
            "confidence": "low" if not desc else "medium",
            "residential": "commercial" not in text.lower(),
            "source": reason,
        }

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        for r in rows:
            results[r["id"]] = fallback(r, "keyword")
        return results

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Permits:\n" + build_prompt(rows)},
            ],
        )
        content = (completion.choices[0].message.content or "")
        parsed = _extract_json(content)
        # The model is told to return a bare array; some models wrap it in an
        # object, so also accept {"permits": [...]}.
        if not parsed and content.strip().startswith("{"):
            try:
                obj = json.loads(content)
                for key in ("permits", "results", "items"):
                    if isinstance(obj.get(key), list):
                        parsed = obj[key]
                        break
            except (ValueError, TypeError):
                parsed = []
    except Exception as exc:
        print(f"[permit-scope] model call failed ({exc}); using keywords", flush=True)
        for r in rows:
            results[r["id"]] = fallback(r, "keyword")
        return results

    by_id = {}
    for item in parsed:
        if isinstance(item, dict) and item.get("id") is not None:
            try:
                by_id[int(item["id"])] = item
            except (TypeError, ValueError):
                continue

    valid_cats = set(CATEGORIES)
    for r in rows:
        item = by_id.get(int(r["id"]))
        if not item or not str(item.get("summary") or "").strip():
            results[r["id"]] = fallback(r, "keyword")
            continue
        cat = str(item.get("category") or "other").lower().strip()
        size = str(item.get("size") or "unknown").lower().strip()
        conf = str(item.get("confidence") or "medium").lower().strip()
        results[r["id"]] = {
            "summary": str(item["summary"])[:200],
            "category": cat if cat in valid_cats else keyword_category(
                f"{r.get('work_type')} {r.get('job_description')}"),
            "size": size if size in ("small", "medium", "large", "unknown") else "unknown",
            "confidence": conf if conf in ("high", "medium", "low") else "medium",
            "residential": bool(item.get("residential", True)),
            "source": "openai",
        }
    return results
