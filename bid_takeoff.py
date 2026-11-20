"""AI takeoff extraction from uploaded bid documents.

Extracts text from uploaded PDFs using PyMuPDF, then sends it to OpenAI to
parse into structured line items (quantities, materials, units, descriptions).
The extracted items are inserted as bid_costs rows with estimated costs.

This supplements the filename-based CSI section parsing in bid_scope_skeleton.py.
That module seeds a checklist of trades; this module fills in quantities and rates
from the actual document content.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import bid_costing

# OpenAI model for takeoff extraction. Uses the same key as the rest of the app.
MODEL = os.getenv("TAKEOFF_MODEL", "gpt-4o-mini")

# Maximum pages to extract per document. Keeps token usage bounded.
MAX_PAGES = int(os.getenv("TAKEOFF_MAX_PAGES", "20"))

# Maximum characters of extracted text to send to the model.
MAX_TEXT_CHARS = int(os.getenv("TAKEOFF_MAX_TEXT_CHARS", "30000"))


def extract_pdf_text(file_path: str, max_pages: int = MAX_PAGES) -> str:
    """Extract text from a PDF file using PyMuPDF. Returns concatenated text."""
    try:
        import pymupdf
    except ImportError:
        return ""
    try:
        doc = pymupdf.open(file_path)
        pages = []
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            text = page.get_text("text")
            if text and text.strip():
                pages.append(f"--- Page {i + 1} ---\n{text.strip()}")
        doc.close()
        return "\n\n".join(pages)
    except Exception as exc:
        print(f"[takeoff] PDF extraction failed for {file_path}: {exc}", flush=True)
        return ""


def parse_line_items_with_ai(text: str) -> list[dict]:
    """Send extracted text to OpenAI and parse into structured line items.

    Returns a list of dicts with keys: description, quantity, unit, unit_cost, category.
    Returns an empty list if the model is unavailable or returns unparseable output.
    """
    if not text or not text.strip():
        return []

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        print("[takeoff] OPENAI_API_KEY not set, skipping AI extraction", flush=True)
        return []

    # Truncate text to stay within token limits
    text = text[:MAX_TEXT_CHARS]

    prompt = f"""You are a construction estimator. Extract line items from this bid document text.

For each line item, extract:
- description: What the item is (e.g., "2x4 studs", "drywall sheets", "labor - framing")
- quantity: How many (number only)
- unit: Unit of measure (e.g., "sf", "lf", "ea", "cy", "lf", "sq", "hrs")
- unit_cost: Estimated cost per unit in dollars (number only, use current Hampton Roads VA pricing)
- category: Trade category — must be one of: labor, material, sub, equipment, permit, other

Return ONLY a valid JSON array of objects. Each object must have exactly these keys:
["description", "quantity", "unit", "unit_cost", "category"]

If you cannot extract meaningful line items, return an empty array [].

Document text:
{text}"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a construction estimator. Extract line items as JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
            max_tokens=4000,
        )
        content = (response.choices[0].message.content or "").strip()
        # Strip markdown code fences if present
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        items = json.loads(content)
        if not isinstance(items, list):
            return []
        # Validate and clean each item
        result = []
        for item in items:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description", "")).strip()
            if not desc:
                continue
            try:
                qty = float(item.get("quantity", 0) or 0)
            except (TypeError, ValueError):
                qty = 0
            try:
                unit_cost = float(item.get("unit_cost", 0) or 0)
            except (TypeError, ValueError):
                unit_cost = 0
            unit = str(item.get("unit", "ea")).strip()[:20]
            category = str(item.get("category", "other")).strip()[:40]
            result.append({
                "description": desc[:200],
                "quantity": qty,
                "unit": unit,
                "unit_cost": unit_cost,
                "category": category,
                "estimated": round(qty * unit_cost, 2),
            })
        return result
    except Exception as exc:
        print(f"[takeoff] AI extraction failed: {exc}", flush=True)
        return []


def extract_and_seed(conn, bid_id: int, company: str, file_paths: list[str]) -> dict:
    """Extract line items from uploaded PDFs and insert them as bid cost lines.

    Returns a summary dict with counts of extracted items and any errors.
    """
    result = {"extracted": 0, "inserted": 0, "errors": []}

    all_items = []
    for path in file_paths:
        if not path.lower().endswith(".pdf"):
            continue
        text = extract_pdf_text(path)
        if not text:
            continue
        items = parse_line_items_ai(text)
        all_items.extend(items)

    if not all_items:
        result["errors"].append("No line items extracted from documents")
        return result

    result["extracted"] = len(all_items)

    # Insert each item as a cost line
    for item in all_items:
        # Map AI category to one of the valid COST_CATEGORIES
        cat = item["category"].lower()
        if cat not in ("labor", "material", "sub", "equipment", "permit", "other"):
            # Heuristic: trades with "labor" in the name go to labor, else material
            if "labor" in cat or "install" in cat:
                cat = "labor"
            else:
                cat = "material"
        try:
            bid_costing.add_cost_line(
                conn, bid_id,
                category=cat,
                description=item["description"],
                qty=item["quantity"] or None,
                unit_cost=item["unit_cost"] or None,
                estimated=item["estimated"] or None,
                unit=item["unit"],
                company=company,
            )
            result["inserted"] += 1
        except Exception as exc:
            result["errors"].append(f"insert failed: {exc}")

    return result


# Alias for compatibility
parse_line_items_ai = parse_line_items_with_ai
