"""AI and instant quotes must read in whole dollars, never cents.

The estimate engine keeps cents internally for arithmetic, but a customer or
caller must never see ``$78,750.00`` or a raw ``7875000``. The instant quote
renders ``low_est``/``high_est`` and the per-square-foot rate; the materials
sandbox formats cents in JavaScript; the voice assistant speaks tool results.
Each surface is checked here.
"""

import re
from pathlib import Path

import jinja2

import construction_main as cm
import estimating_service

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "templates" / "construction"


def _content_block(name: str) -> str:
    source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    match = re.search(r"{%\s*block content\s*%}(.*?){%\s*endblock\s*%}", source, re.S)
    assert match, f"{name} has no content block"
    return match.group(1)


def _render_instant_quote() -> str:
    env = jinja2.Environment(autoescape=True)
    env.filters.update(cm.templates.env.filters)
    template = env.from_string(_content_block("instant_quote.html"))
    return template.render(
        co={"name": "BuildStack Construction", "phone": "(757) 846-9275"},
        project_types=["Kitchen Remodel"],
        project_type="Kitchen Remodel",
        address="",
        rental_intent="",
        sqft=200,
        name="",
        phone="",
        email="",
        quote={
            "model_key": "kitchen",
            "label": "Kitchen Remodel",
            "unit": "interior sq ft",
            "quantifier": "200 interior sq ft",
            "sqft": 200,
            "rate_low": 45.0,
            "rate_high": 90.0,
            "low_est": 78750,
            "high_est": 157500,
            "notes": [],
            "disclaimer": "Not a bid.",
        },
        discount_amount=0,
        coupon_code="",
        prop=None,
        lead_id=None,
        error=None,
    )


# --- the estimate model ------------------------------------------------------

def test_estimates_expose_whole_dollar_amounts():
    est = estimating_service.estimate("kitchen", None, 200)
    assert est is not None
    assert isinstance(est["low_est"], (int, float))
    assert isinstance(est["high_est"], (int, float))
    assert float(est["low_est"]).is_integer()
    assert float(est["high_est"]).is_integer()
    # The cents fields stay exact for arithmetic, but the display fields are
    # whole dollars and agree with them.
    assert round(est["low_cents"] / 100) == est["low_est"]
    assert round(est["high_cents"] / 100) == est["high_est"]


def test_money_filter_rounds_to_whole_dollars():
    assert cm._money(125000) == "$1,250"
    assert cm._money(7875000) == "$78,750"
    assert cm._money(15750000) == "$157,500"


# --- the instant quote page --------------------------------------------------

def test_instant_quote_template_renders_whole_dollars():
    html = _render_instant_quote()
    assert "$78,750" in html
    assert "$157,500" in html
    assert "$78,750.00" not in html
    assert "$157,500.00" not in html
    # The "math" line used to show $45.00–$90.00 / sq ft.
    assert "$45–$90 / sq ft" in html
    assert "$45.00" not in html
    assert "$90.00" not in html


# --- the materials quote form (client-side cents -> dollars) -----------------

def test_quote_form_money_formatter_uses_whole_dollars():
    source = (TEMPLATE_DIR / "quote_form.html").read_text(encoding="utf-8")
    match = re.search(r"function money\(c\)\s*\{(.*?)\}", source, re.S)
    assert match, "quote_form.html has no money() formatter"
    body = match.group(1)
    assert "minimumFractionDigits: 0" in body
    assert "maximumFractionDigits: 0" in body
    assert "minimumFractionDigits: 2" not in body
    assert "maximumFractionDigits: 2" not in body


def test_account_quote_totals_use_the_whole_dollar_filter():
    source = (TEMPLATE_DIR / "account.html").read_text(encoding="utf-8")
    total_row = next(line for line in source.splitlines() if "job_price_cents" in line)
    assert "|money" in total_row
    assert ".2f" not in total_row


def test_photo_quote_money_formatter_uses_whole_dollars():
    source = (TEMPLATE_DIR / "photo_quote.html").read_text(encoding="utf-8")
    match = re.search(r"function money\(cents\)\s*\{(.*?)\}", source, re.S)
    assert match, "photo_quote.html has no money() formatter"
    body = match.group(1)
    assert "maximumFractionDigits: 0" in body
    assert "minimumFractionDigits: 2" not in body


# --- the AI voice/chat quote tools ------------------------------------------

def test_ai_quote_project_speaks_whole_dollars():
    text = cm._swaig_tool_response_text("quote_project", {
        "ok": True,
        "label": "Whole-Home Renovation",
        "project_type": "Whole-Home Renovation",
        "quantifier": "1,750 interior sq ft",
        "low_est": 78750,
        "high_est": 157500,
        "material_notes": [],
        "notes": [],
        "assumed_sqft": False,
        "needs_sqft": False,
    })
    assert "$78,750 to $157,500" in text
    assert ".00" not in text


def test_ai_material_price_speaks_whole_dollars():
    text = cm._swaig_tool_response_text("get_material_price", {
        "ok": True, "sku": "SKU-1", "price_dollars": 12.49,
    })
    assert "$12" in text
    assert "$12.49" not in text


def test_ai_material_search_prices_are_whole_dollars():
    text = cm._swaig_tool_response_text("search_materials", {
        "items": [{
            "brand": "Acme", "name": "Cabinet", "color": "White",
            "sku": "SKU-1", "price_dollars": 12.49, "unit": "each",
            "category": "cabinets", "store": "Home Depot",
        }],
    })
    assert "$12" in text
    assert "$12.49" not in text
