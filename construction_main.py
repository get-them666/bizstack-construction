"""BuildStack Construction LLC — FastAPI web app.

Sister company to Broom Service (short-term rental operations). This site is a
licensed general contractor's marketing + lead engine:

- Public marketing site with services, portfolio, about, and a free-estimate form
- Lead pipeline (new -> contacted -> quoted -> deposit -> in_progress -> completed)
- Stripe project deposits (reserve a project / book a site survey)
- 24/7 AI phone & SMS assistant that captures leads (SignalWire + OpenAI)
- Admin dashboard, appearance/theme controls, and an owner copilot

Same stack and conventions as the Broom Service app, in its own repo + deploy.
"""

import os
import io
import csv
import html
import json
import logging
import re
import uuid
import hmac
import hashlib
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import asyncio
import psycopg
from psycopg.rows import dict_row
try:
    from psycopg_pool import ConnectionPool
except ImportError:  # psycopg_pool is optional; falls back to per-request connections
    ConnectionPool = None
from fastapi import FastAPI, Request, Form, UploadFile, File, Query, Response, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosmtplib
from email.message import EmailMessage

from con_ai_agent import BusinessAIAgent, ModelUnavailable, ToolBudgetExhausted
from loan_outreach import start_outreach_tasks
import brand
import documents_service
import team_messages
import settings_status
import today
import followups
import call_list
import zoho_oauth
import zoho_campaigns
import copilot_memory
import lead_pipeline
import outreach_queue
import copilot_tasks
import copilot_ops
from stripe_service import StripeService, default_deposit_cents
import permit_enricher
import permit_stats
import email_fetcher
import bid_costing
import auth0_service
import auth0_login
import material_picker
import takeoff
import supplier_prices
import ledger
import ledger_sync
import ledger_bridge
import funding_referrals
import bid_opportunities
import bid_sources
import enrichment
from signalwire_service import SignalWireService
import vapi_service
import zoho_sync
import property_service
import estimating_service
import auto_reply
import permit_service
import site_theme
import auth_service
import construction_radar
import materials_service
import supplier_adapters
import training_service
import lead_sources
import scrap_io
import inbound_email
import google_oauth
import google_maps
import photo_store
import autodesk_oauth
import skiptrace_service
import skip_sherpa_service
import pdl_contact_service
import resimply_service
# Lead pipeline (lifecycle, intake, work queue, store). Vendored in this repo at
# bizstack_leads/ -- it is this company's own code, not a dependency on another
# repository. See the lead routes below for the cutover.
from bizstack_leads import lifecycle as lead_lifecycle
from bizstack_leads import intake as lead_intake
from bizstack_leads import queue as lead_queue
from bizstack_leads import store as lead_store
import accurate_append
import abc_supply
import lowes_api
import materials_service
import quoting
import quote_documents
import suppression
logger = logging.getLogger("construction")

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/buildstack")
templates = Jinja2Templates(directory="templates/construction")
stripe_svc = StripeService()
signalwire = SignalWireService()
# The number inbound calls/texts arrive from, i.e. the "recipient" for an inbound
# row. It used to be the literal string 'system', making inbound unattributable.
SIGNALWIRE_NUMBER = os.getenv("SIGNALWIRE_PHONE", "") or "system"
vapi = vapi_service.VapiService(default_base="https://construction.bizstackperks.com")
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "America/New_York"))

LEAD_STATUSES = ["new", "contacted", "quoted", "deposit", "in_progress", "completed", "lost"]
STATUS_LABELS = {
    "new": "New", "contacted": "Contacted", "quoted": "Quoted", "deposit": "Deposit paid",
    "in_progress": "In progress", "completed": "Completed", "lost": "Lost",
}

# Permits carry no phone or email, so they can never move through the pipeline
# above. They get their own actions instead: work the address, then record the
# contact you made. "closed" is legacy data from the old permit page and is
# shown as Archived so it stays visible without cluttering the active flow.
PERMIT_STATUSES = ["new", "mailed", "knocked", "contacted", "won", "lost", "closed"]
PERMIT_STATUS_LABELS = {
    "new": "Not worked", "mailed": "Mailed", "knocked": "Knocked",
    "contacted": "Reached them", "won": "Won the job", "lost": "Lost",
    "closed": "Archived",
}
PERMIT_STATUS_COLORS = {
    "new": "amber", "mailed": "sky", "knocked": "violet", "contacted": "blue",
    "won": "emerald", "lost": "slate", "closed": "slate",
}


# --- Lead source visibility ------------------------------------------------
# Scrap.io is the only gated source left. The federal (SAM.gov) scanner was
# removed outright on 2026-10-13 along with its rows -- the operator does not
# pursue government work -- so there is no source here to toggle back on.
def hidden_lead_sources() -> list:
    """Lead `source` values to exclude from owner-facing lists and counts.

    Each source is gated on its own env var, so turning one off hides only its
    own leads. SAM.gov is gone entirely (see lead_sources) -- the operator does
    not pursue government work -- so it is no longer a toggle here.
    """
    hidden = []
    if not scrap_io.enabled():
        hidden.append("scrap-io")
    return hidden


def source_visibility_clause(alias: str = "") -> str:
    """SQL fragment excluding hidden sources. Returns '' when nothing is hidden."""
    hidden = hidden_lead_sources()
    if not hidden:
        return ""
    pfx = f"{alias}." if alias else ""
    return " AND " + " AND ".join(f"COALESCE({pfx}source, '') <> %s" for _ in hidden)


def source_visibility_params() -> list:
    return list(hidden_lead_sources())


# --- contacts we ALREADY hold, keyed to a street address ---------------------
def _known_contacts_for(cur, street, city, state, zip_code) -> list:
    """Contacts already held for one address. [] when there are none.

    The single-address form of contacts_at_addresses(), for the two owner-lookup
    routes. Same filters, same reason: this is our own data, not a vendor's.
    """
    row_id = contacts_at_addresses(cur, [{"id": 0, "address": (
        f"{street or ''}, {city or ''}, {state or ''} {zip_code or ''}").strip()}])
    return [dict(c) for c in (row_id.get(0) or [])]


def _permit_address_parts(row: dict) -> dict:
    """Combine a permit's situs address with city/state/ZIP stored separately."""
    raw = (row.get("address") or "").strip()
    parsed = skiptrace_service.parse_address(raw)
    street = parsed.get("street") if parsed.get("ok") else raw.split(",")[0].strip()
    city = (parsed.get("city") or row.get("city") or "").strip()
    state = (parsed.get("state") or row.get("state") or "").strip().upper()
    zipcode = (parsed.get("zipcode") or row.get("postal_code") or "").strip()
    if city and not parsed.get("city"):
        street = re.sub(
            rf"(?:,\s*|\s+){re.escape(city)}\s*$", "", street or "", flags=re.IGNORECASE
        ).strip(" ,")
    if not street:
        return {"ok": False, "why": "empty street"}
    if not city:
        return {"ok": False, "why": "no city"}
    if len(state) != 2:
        return {"ok": False, "why": "no state"}
    return {
        "ok": True, "street": street, "city": city.upper(),
        "state": state, "zipcode": zipcode,
        "formatted": ", ".join(part for part in [
            street, city.upper(),
            " ".join(value for value in [state, zipcode] if value),
        ] if part),
    }


def contacts_at_addresses(cur, rows, id_key: str = "id",
                          address_key: str = "address") -> dict:
    """Map row id -> contact details already held for that street address.

    WHY THIS EXISTS RATHER THAN A PROVIDER CALL
    The assessor record gives an owner NAME and no way to reach them, which made
    it look like reaching a permit lead required buying a skip-trace vendor.
    But the answer is often already in this table. Lead #1004 is
    "Leonard G Barlow Jr / leonard.barlow@gmail.com" at 8494 LYNN RIVER ROAD --
    the same address the Owner Lookup resolves to "Leonard G Barlow" from
    RentCast. The owner already had the email; nothing on the permit card or the
    lookup page said so, so it was invisible.

    So this reads `leads` rather than calling anyone. No key, no quota, no
    network, and it cannot be rate limited. RentCast is still the right tool for
    the 529 addresses that have NO lead row at all; this is for the ones we have
    already enriched.

    TWO FILTERS ARE LOAD-BEARING
    Placeholder values are excluded: every permit row carries a synthetic
    `@lead.local` tracking address, and treating that as an email would put a
    dead address on the card and make the app look like it had contact data it
    does not. Same for the junk phone tokens ('unknown', 'n/a', '-').

    Hidden sources are excluded too. Scrap.io spends credits and is gated off
    by default, so surfacing its contacts onto permit cards would leak a
    deliberately hidden source through a side door.

    One query for the whole page, not one per row. Contacts are deduped per
    address: the same person can hold several rows at one house.
    """
    wanted = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        parsed = skiptrace_service.parse_address(r.get(address_key) or "")
        if not parsed["ok"]:
            continue
        key = skiptrace_service.normalize_address(
            parsed["street"], parsed["city"], parsed["state"], parsed["zipcode"])
        wanted.setdefault(key, []).append(r.get(id_key))

    if not wanted:
        return {}

    cur.execute(
        "SELECT name, email, phone, status, source, address FROM leads "
        "WHERE address IS NOT NULL AND BTRIM(address) <> '' "
        "  AND ((email IS NOT NULL AND BTRIM(email) <> '' "
        "        AND LOWER(email) NOT LIKE '%%@lead.local') "
        "    OR (phone IS NOT NULL AND BTRIM(phone) <> '' "
        "        AND LOWER(BTRIM(phone)) NOT IN ('unknown','n/a','none','-','none'))) "
        + source_visibility_clause() + ";",
        tuple(source_visibility_params()),
    )

    bucket: dict = {}
    for row in cur.fetchall():
        parsed = skiptrace_service.parse_address(row.get("address") or "")
        if not parsed["ok"]:
            continue
        key = skiptrace_service.normalize_address(
            parsed["street"], parsed["city"], parsed["state"], parsed["zipcode"])
        if key not in wanted:
            continue
        email = (row.get("email") or "").strip().lower()
        if email.endswith("@lead.local"):
            email = ""
        phone = (row.get("phone") or "").strip()
        if phone.lower() in ("unknown", "n/a", "none", "-"):
            phone = ""
        if not email and not phone:
            continue
        # Same person, several rows at one house (permits re-ingested). Key the
        # dedupe on the contact itself so the card shows one line, not three.
        ident = (email, phone)
        if any(c["_ident"] == ident for c in bucket.setdefault(key, [])):
            continue
        bucket[key].append({
            "_ident": ident,
            "name": (row.get("name") or "").strip(),
            "email": email,
            "phone": phone,
            "status": row.get("status") or "",
            "source": row.get("source") or "",
        })

    out = {}
    for key, ids in wanted.items():
        contacts = bucket.get(key)
        if contacts:
            for row_id in ids:
                out[row_id] = contacts
    return out


# --- Company identity (env-driven so both brands stay configurable) ---------
def company() -> dict:
    return {
        # Registered 2026-10-28 as BuildStack Construction LLC (SCC 12097281);
        # brand.py is the single source for the name and entity details.
        "name": brand.name(),
        "short_name": brand.short_name(),
        "entity_id": brand.entity_id(),
        "entity_line": brand.entity_line(),
        "address": brand.mailing_address(),
        "phone": os.getenv("COMPANY_PHONE", "+1 (757) 908-7121"),
        "phone_e164": os.getenv("VAPI_PHONE", "+17579087121"),
        "email": os.getenv("COMPANY_EMAIL", "hello@bizstackperks.com"),
        "domain": os.getenv("COMPANY_DOMAIN", "construction.bizstackperks.com"),
        "license": os.getenv("CONTRACTOR_LICENSE", ""),
        "service_area": os.getenv(
            "SERVICE_AREA",
            "Williamsburg–Hampton Roads, VA · Currituck County & Elizabeth City, NC",
        ),
        # "Founded 2026" contradicted the owner's own copy -- 32 years in the
        # trade, six of them as a federal construction inspector. The company
        # formed recently; the experience did not.
        "founded": "2026",
    }


def _money(value):
    try:
        return f"${float(value or 0) / 100:,.0f}"
    except (TypeError, ValueError):
        return "—"


templates.env.filters["money"] = _money


@asynccontextmanager
async def lifecycle(app: FastAPI):
    print("📡 Initing cloud-native table migration layer check...")
    # Warm the pool before the startup DDL block and the background threads, so
    # the first real request isn't the one paying to build a connection.
    _db_pool()
    try:
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    email VARCHAR(255),
                    project_type VARCHAR(120),
                    address VARCHAR(255),
                    budget VARCHAR(120),
                    timeline VARCHAR(120),
                    description TEXT,
                    source VARCHAR(50) DEFAULT 'website',
                    company VARCHAR(40) DEFAULT 'construction',
                    status VARCHAR(50) DEFAULT 'new',
                    deposit_status VARCHAR(50) DEFAULT 'none',
                    deposit_cents INTEGER,
                    stripe_session_id VARCHAR(255),
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS company VARCHAR(40) DEFAULT 'construction';")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_status VARCHAR(50) DEFAULT 'none';")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR(255);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS draft_reply TEXT;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER REFERENCES leads(id),
                    stripe_session_id VARCHAR(255),
                    stripe_payment_intent_id VARCHAR(255),
                    amount_cents INTEGER NOT NULL,
                    currency VARCHAR(10) DEFAULT 'usd',
                    company VARCHAR(40) DEFAULT 'construction',
                    status VARCHAR(50) NOT NULL DEFAULT 'paid',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS company VARCHAR(40) DEFAULT 'construction';")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS login_otps (
                    id SERIAL PRIMARY KEY,
                    role VARCHAR(50) NOT NULL,
                    actor_id INTEGER,
                    email VARCHAR(255),
                    code_hash VARCHAR(255) NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    consumed BOOLEAN DEFAULT FALSE,
                    expires_at TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS comms_logs (
                    id SERIAL PRIMARY KEY,
                    direction VARCHAR(10) NOT NULL,
                    channel VARCHAR(20) NOT NULL,
                    sender VARCHAR(255) NOT NULL,
                    recipient VARCHAR(255) NOT NULL,
                    message_body TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # Attribute each outbound touch to the lead it was made for, not
                # just the address it was sent to. Without this the contact policy
                # can only count per (channel, recipient), so a lead mailed a
                # letter on channel 'mail' starts that channel's counter at zero
                # and could be mailed twice AND emailed twice - four contacts
                # against a once-then-5-days rule. Nullable on purpose: historical
                # rows stay NULL and the policy falls back to recipient counting
                # for them, so this backfills forward instead of resetting.
                #
                # This ALTER has to sit AFTER the CREATE above. It used to run ~40
                # lines earlier, so on an empty database it failed with
                # 'relation "comms_logs" does not exist' -- and because the whole
                # block is one transaction, the rollback also discarded the leads
                # table created before it. The app could not bootstrap a fresh
                # database at all; it only ever worked in production because
                # comms_logs had existed there for months.
                cur.execute("ALTER TABLE comms_logs ADD COLUMN IF NOT EXISTS lead_id INTEGER;")
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS idx_comms_logs_lead
                    ON comms_logs (lead_id, direction, created_at)
                    WHERE lead_id IS NOT NULL;
                """)
                inbound_email._ensure_schema(cur)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) DEFAULT 'admin',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(255) DEFAULT '';")
                cur.execute("ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS property_sqft INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_low_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_high_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_json TEXT;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS supplies_inventory (
                    id SERIAL PRIMARY KEY,
                    company VARCHAR(40) NOT NULL DEFAULT 'construction',
                    item_name VARCHAR(255) NOT NULL,
                    sku VARCHAR(120),
                    category VARCHAR(120),
                    qty_on_hand NUMERIC(12,2) NOT NULL DEFAULT 0,
                    unit VARCHAR(50) DEFAULT 'each',
                    reorder_level NUMERIC(12,2) NOT NULL DEFAULT 0,
                    notes TEXT,
                    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("CREATE INDEX IF NOT EXISTS idx_supplies_company ON supplies_inventory(company);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS listing_url VARCHAR(512);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS campaign VARCHAR(80);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS referral_code VARCHAR(120);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS analysis_json TEXT;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_needed BOOLEAN DEFAULT FALSE;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_use TEXT;")
                # The /finance form asks for an amount. This column already exists
                # on the live table but nothing created it on a fresh database,
                # so a new environment would silently drop the field.
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS funding_amount_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS partner_id INTEGER;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS lead_posts (
                    post_id TEXT PRIMARY KEY,
                    subreddit TEXT,
                    created_utc TEXT,
                    lead_id INTEGER
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS lead_source_seen (
                    source VARCHAR(40) NOT NULL,
                    external_id TEXT NOT NULL,
                    lead_id INTEGER,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (source, external_id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS crew (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    email VARCHAR(255),
                    role VARCHAR(50) DEFAULT 'crew',
                    pay_type VARCHAR(20) DEFAULT 'hourly',
                    pay_rate NUMERIC(10,2) DEFAULT 0,
                    pin_hash VARCHAR(255),
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS stripe_account_id VARCHAR(255);")
                cur.execute("ALTER TABLE crew ADD COLUMN IF NOT EXISTS bank_status VARCHAR(20) DEFAULT 'none';")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER REFERENCES leads(id),
                    name VARCHAR(255) NOT NULL,
                    address VARCHAR(255),
                    summary TEXT,
                    start_date DATE,
                    end_date DATE,
                    status VARCHAR(50) DEFAULT 'planned',
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS project_assignments (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crew(id) ON DELETE CASCADE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (project_id, crew_id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS clock_events (
                    id SERIAL PRIMARY KEY,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    project_id INTEGER REFERENCES projects(id),
                    punch_in_at TIMESTAMP WITH TIME ZONE,
                    punch_out_at TIMESTAMP WITH TIME ZONE,
                    punch_in_lat NUMERIC(9,6),
                    punch_in_lng NUMERIC(9,6),
                    punch_out_lat NUMERIC(9,6),
                    punch_out_lng NUMERIC(9,6),
                    work_type VARCHAR(20) DEFAULT 'regular',
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS timesheets (
                    id SERIAL PRIMARY KEY,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    project_id INTEGER REFERENCES projects(id),
                    work_date DATE NOT NULL,
                    start_time VARCHAR(10),
                    end_time VARCHAR(10),
                    hours NUMERIC(6,2) NOT NULL,
                    work_type VARCHAR(20) DEFAULT 'regular',
                    source VARCHAR(20) DEFAULT 'manual',
                    status VARCHAR(20) DEFAULT 'submitted',
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_runs (
                    id SERIAL PRIMARY KEY,
                    period_start DATE NOT NULL,
                    period_end DATE NOT NULL,
                    status VARCHAR(20) DEFAULT 'open',
                    note TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payroll_lines (
                    id SERIAL PRIMARY KEY,
                    run_id INTEGER NOT NULL REFERENCES payroll_runs(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crew(id),
                    hours NUMERIC(6,2) DEFAULT 0,
                    overtime_hours NUMERIC(6,2) DEFAULT 0,
                    gross_cents INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (run_id, crew_id)
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS job_photos (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER REFERENCES projects(id),
                    crew_id INTEGER REFERENCES crew(id),
                    filename VARCHAR(500) NOT NULL,
                    caption VARCHAR(500),
                    is_punchlist BOOLEAN DEFAULT FALSE,
                    uploaded_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS job_leads (
                    id SERIAL PRIMARY KEY,
                    permit_number VARCHAR(255),
                    address VARCHAR(255),
                    city VARCHAR(120),
                    state VARCHAR(5),
                    work_type VARCHAR(120),
                    job_description TEXT,
                    contractor_name VARCHAR(255),
                    issue_date VARCHAR(40),
                    estimated_value NUMERIC(14,2),
                    owner_contact VARCHAR(50),
                    status VARCHAR(20) DEFAULT 'new',
                    source VARCHAR(50) DEFAULT 'permits',
                    is_demo BOOLEAN DEFAULT FALSE,
                    found_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                # Must come after the CREATE above: an ALTER on a table that does
                # not exist yet raises and aborts the whole schema init.
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS scope_summary TEXT;")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS scope_category VARCHAR(60);")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS scope_confidence VARCHAR(20);")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS scope_researched_at TIMESTAMP WITH TIME ZONE;")
                # Both city feeds publish a residential/commercial signal (Norfolk
                # use_class, Virginia Beach ConstructionType). wants_lead() read it
                # at ingest and then the INSERT dropped it, so it could never be
                # displayed or re-filtered afterwards. Kept now.
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS property_type VARCHAR(120);")
                # use_class is the normalized verdict ('residential' /
                # 'commercial' / 'unknown'). Kept separate from property_type,
                # which holds the raw feed value: Virginia Beach packs values
                # like 'Roof and or Siding' and 'Asbestos' into that column, so
                # overloading it destroyed the distinction between source data
                # and our own classification.
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS use_class VARCHAR(20);")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS postal_code VARCHAR(10);")
                # Geocoding results. Nullable: the list must stay fully usable when
                # Google Maps is unconfigured.
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS lat DOUBLE PRECISION;")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS lng DOUBLE PRECISION;")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS geocode_status VARCHAR(30);")
                cur.execute("ALTER TABLE job_leads ADD COLUMN IF NOT EXISTS geocoded_at TIMESTAMP WITH TIME ZONE;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS generated_documents (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    category VARCHAR(50),
                    file_name VARCHAR(500),
                    file_type VARCHAR(20) DEFAULT 'pdf',
                    file_data BYTEA,
                    sent_email VARCHAR(255),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("""
                CREATE TABLE IF NOT EXISTS worker_quiz_results (
                    id SERIAL PRIMARY KEY,
                    worker_id INTEGER,
                    worker_name VARCHAR(255) NOT NULL,
                    email VARCHAR(255),
                    score INTEGER NOT NULL,
                    total INTEGER NOT NULL,
                    passed BOOLEAN NOT NULL,
                    answers_json TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE worker_quiz_results ADD COLUMN IF NOT EXISTS test_slug VARCHAR(60);")
                cur.execute("ALTER TABLE worker_quiz_results ADD COLUMN IF NOT EXISTS test_title VARCHAR(255);")
            conn.commit()
        print("✅ Schema ready.")
    except Exception as e:
        print(f"⚠️ Database init skipped: {e}")

    try:
        with psycopg.connect(db_url) as conn:
            count = materials_service.refresh_catalog(conn)
            print(f"📦 materials_catalog ready ({count} rows).")
    except Exception as e:
        print(f"⚠️ materials_catalog refresh skipped: {e}")

    # Said at boot, not discovered after a deploy wiped the crew's photos.
    try:
        photo_store.startup_check()
    except Exception as e:
        print(f"⚠️ photo storage check failed: {e}", flush=True)

    # Bid board is its own module with its own idempotent DDL, so it is not in
    # the startup block above -- that block is one transaction and an ordering
    # slip there rolls back 52 tables. Rows are entered through /bids, never
    # seeded at boot: live pipeline does not belong in version control.
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            bid_opportunities.ensure_schema(conn)
            # Order matters and is not arbitrary: costing extends
            # bid_opportunities with outcome columns and a projects FK, so it
            # runs after. takeoff and supplier_prices are independent of each
            # other and guard their own FKs.
            bid_costing.ensure_accounting_schema(conn)
            takeoff.ensure_schema(conn)
            supplier_prices.ensure_schema(conn)
            # Chart of accounts and journal tables. Its own ensure_schema also
            # seeds the chart, so a new account added to ledger.CHART appears
            # on the next boot without a migration.
            ledger.ensure_schema(conn)
            # Business/SBA referral records behind /finance. Independent of the
            # ledger tables; the commission only reaches the books later, when
            # the owner marks a referral paid.
            funding_referrals.ensure_schema(conn)
        print("🎯 bid_opportunities ready.")
    except Exception as e:
        print(f"⚠️ bid_opportunities init skipped: {e}")

    # Lead pipeline orchestrator: auto-enrich → contact → quote flow
    try:
        with psycopg.connect(db_url) as conn:
            lead_pipeline.ensure_schema(conn)
        print("🔄 Lead pipeline orchestrator ready.")
    except Exception as e:
        print(f"⚠️ Lead pipeline init skipped: {e}")

    if not os.getenv("DISABLE_LOAN_OUTREACH"):
        _outreach_tasks = start_outreach_tasks()
        try:
            app.state.outreach_tasks = _outreach_tasks
        except AttributeError:
            pass

    if not os.getenv("DISABLE_PERMIT_IMPORT"):
        try:
            _start_permit_importer()
        except Exception as exc:
            print(f"[permit-scan] could not start importer: {exc}", flush=True)

    if not os.getenv("DISABLE_LEAD_SCAN"):
        try:
            _start_lead_source_scheduler()
        except Exception as exc:
            print(f"[lead-source] could not start scheduler: {exc}", flush=True)

    # The Vapi assistant lives in the Vapi dashboard, and its custom-LLM URL is
    # whatever domain the app had when the assistant was created. If that URL
    # errors, Vapi still speaks the assistant's firstMessage and then goes
    # silent for the rest of the call -- the phone bot "answers" and then never
    # replies again. Repoint it at this app on every boot so a domain move
    # can't silently kill the voice line.
    def _sync_vapi_assistant():
        try:
            vapi.sync_assistant()
        except Exception as exc:
            print(f"[vapi] assistant url sync failed: {exc}", flush=True)

    threading.Thread(target=_sync_vapi_assistant, daemon=True).start()

    # Copilot conversation memory + the one-year task list. Self-initializing, so
    # this is safe on every boot and on redeploys against an existing database.
    try:
        with psycopg.connect(db_url) as conn:
            copilot_memory._ensure_schema(conn)
            copilot_tasks._ensure_schema(conn)
            swept_msgs = copilot_memory.purge_expired(conn)
            swept_tasks = copilot_tasks.purge_expired(conn)
        print(
            f"🧠 Copilot memory ready (swept {swept_msgs} expired messages, "
            f"{swept_tasks} expired tasks)",
            flush=True,
        )
    except Exception as exc:
        print(f"⚠️ copilot memory init skipped: {exc}", flush=True)

    # Lead pipeline (vendored at bizstack_leads/). Adds the
    # queue-state columns and tags every lead that predates them, so the work
    # queue ("what do I do next?") is a single indexed query. Idempotent: after
    # the first boot the backfill has nothing to do. Imported here rather than at
    # module scope so a problem degrades to a warning, not a boot crash --
    # the same pattern as every other optional subsystem above.
    try:
        from bizstack_leads import schema as leads_schema, store as leads_store
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            leads_schema.ensure_schema(conn)
            tagged = leads_store.backfill(conn, "construction")
        print(f"🧭 Lead pipeline ready ({tagged} lead(s) tagged).", flush=True)
    except Exception as exc:
        print(f"⚠️ lead pipeline init skipped: {exc}", flush=True)

    # Team messages (owner <-> crew). Nothing in the startup DDL ever created
    # this table -- production has it only because it was forked from the
    # shared database, with company DEFAULT 'broom'. A fresh database 500'd
    # on the crew chat without this.
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            team_messages.ensure_schema(conn)
        print("💬 Team messages ready.", flush=True)
    except Exception as exc:
        print(f"⚠️ team messages init skipped: {exc}", flush=True)

    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            zoho_sync.ensure_schema(conn)
            followups.ensure_schema(conn)
    except Exception as exc:
        print(f"⚠️ zoho/followups schema init skipped: {exc}", flush=True)

    # Inbound-lead follow-ups (day 1/3/7 with a booking link). Review mode
    # (LEAD_AUTO_SEND unset) and DISABLE_FOLLOWUPS both stop it.
    try:
        followups.start(_run_followups)
    except Exception as exc:
        print(f"[followups] could not start: {exc}", flush=True)

    # BuildingConnected -> bid board, every few hours, once the owner has
    # signed in to Autodesk. Invitations should land on the board by
    # themselves; the Today screen shows what is due.
    try:
        import bid_sync
        bid_sync.start(_owner_email)
    except Exception as exc:
        print(f"[bid-sync] could not start: {exc}", flush=True)

    # Daily lead-enrichment autopilot: free parcel lookup -> Skip Sherpa (one
    # capped credit) -> conservative write-back -> staged first-touch draft.
    # Research and staging only; nothing sends. Started here, after the DDL
    # block has committed (it needs app_settings), individually guarded like
    # every other background thread.
    try:
        import lead_enrich_loop
        lead_enrich_loop.start()
    except Exception as exc:
        print(f"[lead-enrich] could not start autopilot: {exc}", flush=True)

    try:
        yield
    finally:
        # Drain the pool on shutdown so redeploys don't leave sockets in
        # TIME_WAIT against Postgres.
        close_db_pool()


app = FastAPI(title=brand.name(), lifespan=lifecycle)


@app.exception_handler(Exception)
async def api_exception_handler(request: Request, exc: Exception):
    """Keep unexpected API failures JSON-shaped and correlate them with logs."""
    request_id = uuid.uuid4().hex[:12]
    logger.exception(
        "Unhandled request failure request_id=%s method=%s path=%s",
        request_id, request.method, request.url.path,
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "internal_server_error",
                "detail": "The request failed unexpectedly. Share this reference with support.",
                "request_id": request_id,
            },
        )
    return HTMLResponse(content="Internal server error", status_code=500)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """Clickjacking and sniffing protection on every response.

    /embed/* is the one surface built to be framed by other sites (the
    copy-paste iframe from /embed/snippet), so it is left frameable. Everything
    else -- including the owner dashboard and the Stripe return pages -- is
    restricted to same-origin frames. A full CSP is deliberately not set here:
    the pages carry inline scripts and GA, and a blocking policy would need a
    nonce pipeline to be safe.
    """
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/embed/"):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
# Files already on local disk stay reachable at /uploads/. New uploads go
# through /media/ and survive a redeploy.


@app.get("/media/{key:path}")
async def media_fetch(key: str):
    """Serve an uploaded photo from whichever backend holds it.

    Keys rather than URLs are what records store, so this stays valid across a
    backend change and across the deploys that used to delete local files.
    """
    try:
        body, content_type = photo_store.read(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")
    return Response(content=body, media_type=content_type,
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.get("/api/storage/status")
async def storage_status(request: Request):
    """Whether photos survive a deploy, and what is missing if they do not."""
    require_admin(request)
    return JSONResponse(content=photo_store.status())


WORKER_COOKIE = "worker_session"


# --- DB + auth plumbing -----------------------------------------------------
# Connection pool.
#
# get_db() used to call psycopg.connect() per request, so every page cost a
# full TCP + TLS + auth handshake against Postgres before any SQL ran. It also
# meant a fresh backend process per concurrent request instead of a reused
# session. A pool keeps warm, already-authenticated connections and hands them
# back; psycopg resets the session (rollback, no open txn) on release.
#
# Sized small on purpose: the app is single-process (Procfile runs one uvicorn
# worker), so max_size is a ceiling on concurrency, not a throughput target.
# POOL_MAX_SIZE is the one knob if request concurrency ever goes up.
_pool = None
_pool_warned = False


def _pool_max_size() -> int:
    try:
        return max(1, int(os.getenv("DB_POOL_MAX_SIZE", "10")))
    except (TypeError, ValueError):
        return 10


def _db_pool():
    """Lazily build the pool. Returns None when pooling is unavailable."""
    global _pool, _pool_warned
    if _pool is not None:
        return _pool
    if ConnectionPool is None:
        if not _pool_warned:
            logger.warning("psycopg_pool not installed - falling back to per-request connections")
            _pool_warned = True
        return None
    try:
        _pool = ConnectionPool(
            conninfo=db_url,
            min_size=1,
            max_size=_pool_max_size(),
            timeout=float(os.getenv("DB_POOL_TIMEOUT", "10")),
            kwargs={"row_factory": dict_row},
            open=False,  # opened in lifespan, so a bad DSN can't kill import
        )
        _pool.open()
        _pool.wait(timeout=15)
        logger.info("db pool ready min=1 max=%s", _pool_max_size())
    except Exception as exc:
        if not _pool_warned:
            logger.warning("db pool unavailable (%s) - using per-request connections", exc)
            _pool_warned = True
        _pool = None
    return _pool


def close_db_pool():
    global _pool
    if _pool is not None:
        try:
            _pool.close()
        except Exception:
            pass
        _pool = None


def get_db():
    pool = _db_pool()
    if pool is not None:
        # getconn blocks up to DB_POOL_TIMEOUT, then raises PoolTimeout. Fall
        # back to a direct connection so a saturated pool degrades into slowness
        # instead of a 500.
        try:
            conn = pool.getconn()
        except Exception as exc:
            logger.warning("pool exhausted, opening a direct connection: %s", exc)
        else:
            try:
                yield conn
            finally:
                # Hand the session back rather than closing it. psycopg rolls
                # back any transaction the route left open, so a failed request
                # cannot poison the next one that gets this connection.
                pool.putconn(conn)
            return
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        yield conn
    finally:
        conn.close()


def current_actor(request: Request):
    """Return {role, id, email, name} for a signed-in session, or None."""
    return auth_service.actor_from_token(request.cookies.get(auth_service.SESSION_COOKIE))


def require_auth(request: Request):
    actor = current_actor(request)
    is_admin = bool(actor and actor.get("role") == "admin")
    return is_admin, (actor.get("email") if actor else request.cookies.get("user_email"))


def require_admin(request: Request) -> None:
    if not require_auth(request)[0]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Admin login required")


# --- Auth0 login bridge ------------------------------------------------------
# Auth0 proves who someone is; it does not know who your crew are. This
# answers that on every login so an Action can either attach a role claim or
# deny the login outright.

@app.post("/api/auth/login")
async def auth_login_check(request: Request, db=Depends(get_db)):
    """Called by an Auth0 Action during login. Answers: allowed, and what role.

    Returns 200 with {"allowed": false} for someone who is not on the list --
    not an error status. A 403 here would make the Action's job ambiguous:
    a rejected login and a broken endpoint look identical.
    """
    # A missing secret means an unconfigured deploy, and it must fail closed.
    if not auth0_service.authorized(request.headers.get(auth0_service.HEADER)):
        raise HTTPException(status_code=401, detail="Bad or missing credentials")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        auth0_service.ensure_schema(db)
        verdict = auth0_service.decide(
            db,
            email=str(payload.get("email") or ""),
            name=str(payload.get("name") or "")[:255],
            auth0_sub=str(payload.get("sub") or "")[:120],
            user_agent=request.headers.get("user-agent", ""),
            ip=(request.client.host if request.client else "") or "",
        )
    except Exception as e:
        # Fail closed, and say why. Never let a database hiccup decide who
        # is allowed into the books.
        logger.warning("auth login check failed: %s", e)
        return JSONResponse({"allowed": False, "role": None, "reason": "lookup failed"})
    return JSONResponse(verdict)


@app.get("/api/auth/status")
async def auth_status(request: Request, db=Depends(get_db)):
    """Recent sign-ins. Admin only -- the log shows who has been in."""
    require_admin(request)
    try:
        auth0_service.ensure_schema(db)
        return JSONResponse({
            "ok": True,
            "configured": auth0_service.configured(),
            "owner_emails": len(auth0_service.allowlist()["owners"]),
            "recent": auth0_service.recent(db, 40),
            "denied": auth0_service.denied_recent(db, 20),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


def require_role(request: Request, *roles: str) -> str:
    """Gate a page on an Auth0 role claim.

    Separate from require_admin on purpose: require_admin governs the
    existing PIN/email login and its behaviour must not change. This reads
    auth0_role off the SAME session token, so it works with whichever login
    produced it, and is inert -- denying everything -- until Auth0 is wired.

    Uses auth_service.SESSION_COOKIE rather than a cookie name invented
    here: the admin session is set under that key at login, so anything else
    reads a cookie that is never written and denies everyone.
    """
    actor = current_actor(request)
    got = ((actor or {}).get("auth0_role") or "").strip().lower()
    if not got or got not in [r.lower() for r in roles]:
        raise HTTPException(status_code=403,
                            detail="Your account does not have access here.")
    return got


# --- Auth0 sign-in -----------------------------------------------------------
# Auth0 proves identity; this mints the SAME session cookie the PIN/email
# login issues, so require_admin and every gated page behave identically.
# Nothing downstream knows Auth0 exists.

@app.get("/api/auth/login/start")
async def auth0_login_start(request: Request):
    """Begin Auth0 sign-in. Sets PKCE + state + nonce cookies, redirects out."""
    if not auth0_login.configured():
        return RedirectResponse(
            url="/login?error=" + urllib.parse.quote(
                "Auth0 is not configured: " + ", ".join(auth0_login.missing())),
            status_code=status.HTTP_303_SEE_OTHER)
    base = str(request.base_url).rstrip("/")
    redirect_uri = os.getenv("AUTH0_CALLBACK_URL") or f"{base}/api/auth/callback"
    verifier = auth0_login.new_verifier()
    nonce = auth0_login.new_nonce()
    state = auth0_login.new_verifier()
    try:
        url = auth0_login.authorize_url(verifier, nonce, state, redirect_uri)
    except auth0_login.Auth0Error as e:
        return RedirectResponse(url="/login?error=" + urllib.parse.quote(str(e)[:180]),
                                status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    for name, value in ((auth0_login.STATE_COOKIE, state),
                        (auth0_login.VERIFIER_COOKIE, verifier),
                        (auth0_login.NONCE_COOKIE, nonce)):
        resp.set_cookie(name, value, max_age=auth0_login.COOKIE_TTL,
                        httponly=True, samesite="lax", secure=True, path="/")
    return resp


@app.get("/api/auth/callback")
async def auth0_callback(request: Request, code: str = "", state: str = "",
                         error: str = "", error_description: str = "",
                         db=Depends(get_db)):
    """Receive Auth0's reply, verify it, and sign the person in.

    No admin gate: this is the far side of a redirect, and a login wall here
    would break the flow. Every failure mode is a redirect with a readable
    reason instead of a stack trace.
    """
    got_state = request.cookies.get(auth0_login.STATE_COOKIE, "")
    verifier = request.cookies.get(auth0_login.VERIFIER_COOKIE, "")
    nonce = request.cookies.get(auth0_login.NONCE_COOKIE, "")

    def bail(msg: str):
        r = RedirectResponse(url="/login?error=" + urllib.parse.quote(msg[:180]),
                             status_code=status.HTTP_303_SEE_OTHER)
        auth0_login.clear_cookies(r)
        return r

    if error:
        return bail(error_description or f"Auth0 refused: {error}")
    if not code:
        return bail("No authorization code came back from Auth0.")
    # State is the CSRF check for the redirect. Constant-time, and a missing
    # value on either side is a refusal rather than a pass.
    import hmac as _hmac
    if not got_state or not state or not _hmac.compare_digest(got_state, state):
        return bail("That sign-in did not start here, so it was discarded.")
    if not verifier or not nonce:
        return bail("The sign-in session expired. Try again.")

    base = str(request.base_url).rstrip("/")
    redirect_uri = os.getenv("AUTH0_CALLBACK_URL") or f"{base}/api/auth/callback"
    try:
        auth0_service.ensure_schema(db)
        actor = auth0_login.complete_login(code, verifier, nonce, redirect_uri, db)
    except auth0_login.Auth0Error as e:
        logger.info("auth0 login refused: %s", e)
        return bail(str(e))
    except Exception as e:
        # Fail closed. A stack trace on a login page is both ugly and a leak.
        logger.warning("auth0 login failed: %s", e)
        return bail("Sign-in could not be completed.")

    if actor.get("claim_mismatch"):
        # Not fatal -- the allowlist already decided -- but worth a trail.
        logger.warning("auth0: role claim %s disagreed with the allowlist for %s",
                       actor.get("claim_roles"), actor.get("email"))
    resp = _finish_login(actor, request)
    auth0_login.clear_cookies(resp)
    return resp


# --- Worker (crew) auth -----------------------------------------------------
def current_worker(request: Request):
    """Return worker session actor for the crew portal, or None."""
    token = request.cookies.get(WORKER_COOKIE)
    actor = auth_service.actor_from_token(token) if token else None
    if not actor or actor.get("role") != "worker":
        return None
    return actor


def require_worker(request: Request) -> dict:
    worker = current_worker(request)
    if not worker:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Worker login required")
    return worker


def _hash_pin(pin: str) -> str:
    secret = (auth_service._secret()).decode() if hasattr(auth_service, "_secret") else "dev"
    return hashlib.sha256((str(pin) + "|" + secret).encode()).hexdigest()


def _verify_pin(pin: str, pin_hash: str) -> bool:
    if not pin_hash:
        return False
    candidate = _hash_pin(pin)
    return hmac.compare_digest(candidate, pin_hash or "")


def _worker_login_response(worker: dict) -> RedirectResponse:
    resp = RedirectResponse(url="/crew", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=WORKER_COOKIE,
        value=auth_service.issue_session("worker", worker.get("id"), worker.get("email") or "", worker.get("name") or ""),
        httponly=True, samesite="lax", secure=_secure_cookies(),
    )
    return resp


def _format_currency(cents) -> str:
    try:
        return f"${float(cents or 0) / 100:,.0f}"
    except (TypeError, ValueError):
        return "—"


def purge_pdl_negative_cache(cur) -> int:
    """Delete PDL rows cached as 'no contact found'. Returns the count.

    Needed because the cache deliberately stores misses so a dead address is not
    re-billed, and it cannot tell a real miss from one recorded while the API key
    was rejected. Until this runs, every address that was looked up with a bad key
    keeps answering 'no contact' from cache even after the key is repaired.

    Only negative rows are touched -- real hits are worth keeping.
    """
    cur.execute("DELETE FROM pdl_contact_cache WHERE found = FALSE;")
    return cur.rowcount


def _map_embed(address: str | None) -> str:
    """Iframe map for a job address.

    Was a Google embed, which required a billing-enabled Cloud project. OpenStreet
    Map's export/embed is keyless and drop-in; Apple Maps publishes no iframe
    endpoint at all, so it cannot serve this role.
    """
    import open_geo
    return open_geo.embed_url_for_address(address or "")


def _map_directions(address: str | None) -> str:
    """'Map ->' link for a job address. Apple Maps takes the address directly."""
    import open_geo
    return open_geo.directions_url_for_address("", address or "")


def _template_actor(request: Request):
    return current_actor(request)


def _template_features(request: Request):
    return {}


def _asset_version() -> str:
    """Cache-buster for the built stylesheet: mtime of the file at boot.

    Browsers cache /static aggressively, so a rebuilt tailwind.css would
    otherwise sit behind a stale copy until the user hard-refreshed. Computed
    once; the file only changes with a deploy, which restarts the process.
    """
    try:
        return str(int(os.path.getmtime(os.path.join("static", "css", "tailwind.css"))))
    except OSError:
        return "0"


ASSET_VERSION = _asset_version()

templates.env.globals["current_actor"] = _template_actor
templates.env.globals["current_features"] = _template_features
templates.env.globals["site_theme_state"] = lambda: site_theme.state()
templates.env.globals["company"] = company
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.globals["map_embed"] = _map_embed
templates.env.globals["map_directions"] = _map_directions
templates.env.globals["zoho_salesiq_widget"] = lambda: os.getenv("ZOHO_SALESIQ_WIDGET_CODE", "")


def _owner_email() -> str:
    """The APS token is stored per signed-in owner.

    Single-owner app today, so this resolves to the admin account rather than
    threading an actor through every APS call. When a second admin connects
    their own Autodesk, this becomes current_actor(request)["email"] and the
    token table's UNIQUE(user_email) already carries it.
    """
    return os.getenv("ADMIN_EMAIL", "hello@bizstackperks.com")


def admin_email() -> str:
    """The primary owner address. For membership, use ``admin_emails()``."""
    return os.getenv("ADMIN_EMAIL", "hello@bizstackperks.com")


def admin_emails() -> set:
    """Every address that should land on the admin side of a sign-in.

    ADMIN_EMAIL holds one address, and an owner signs in with more than one
    over time: a personal Gmail and a Workspace mailbox on the same domain.
    With a single-address check the second one is silently downgraded to an
    ordinary user, which is how an owner gets locked out of their own
    dashboard with no error anywhere.

    Reads ADMIN_EMAIL plus the comma-separated ADMIN_EMAILS, so existing
    single-address deployments are unchanged. Mirrors
    auth0_service.allowlist()["owners"].
    """
    out = set()
    for chunk in (os.getenv("ADMIN_EMAILS", "") or "").split(","):
        if chunk.strip():
            out.add(chunk.strip().lower())
    primary = (os.getenv("ADMIN_EMAIL", "") or "").strip()
    if primary:
        out.add(primary.lower())
    return out


def is_admin_email(email: str) -> bool:
    return bool(email) and email.strip().lower() in admin_emails()


def _secure_cookies() -> bool:
    return os.getenv("COOKIE_SECURE", "false").lower() == "true"


def _login_redirect(role: str) -> str:
    return {"admin": "/pipeline", "customer": "/account"}.get(role or "", "/")


def _site_base(request: Request) -> str:
    return f"https://{company()['domain']}".rstrip("/") or str(request.base_url).rstrip("/")


# --- Settings helpers -------------------------------------------------------
def _get_setting(db, key, default=""):
    with db.cursor() as cur:
        cur.execute("SELECT value FROM app_settings WHERE key = %s;", (key,))
        row = cur.fetchone()
    return row["value"] if row else default


def _set_setting(db, key, value):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, NOW()) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW();",
            (key, value),
        )
        db.commit()


# --- Crew feature toggles (namespaced per-role) ------------------------------
CREW_PERMS_KEY = "perms_crew"


def _crew_features(db) -> dict:
    return auth_service.features_for("worker", _get_setting(db, CREW_PERMS_KEY, ""))


def _require_crew_feature(db, feature: str):
    if not bool(_crew_features(db).get(feature)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This tool is turned off for your account")


def _otp_enabled(db) -> bool:
    env = os.getenv("OTP_ENABLED", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if env in ("1", "true", "yes", "on"):
        return True
    return _get_setting(db, "otp_enabled", "true").strip().lower() not in ("0", "false", "no", "off")


def _resolve_credentials(db, identifier: str, secret: str):
    ident = (identifier or "").strip()
    secret = (secret or "").strip()
    admin_email = os.getenv("ADMIN_EMAIL", "hello@bizstackperks.com")
    admin_password = os.getenv("ADMIN_PASSWORD", "password123")
    if is_admin_email(ident) and secret == admin_password:
        return {
            "role": "admin", "id": 0,
            "email": (os.getenv("ADMIN_OTP_EMAIL") or admin_email),
            "name": os.getenv("ADMIN_NAME", "Owner"),
        }
    return None


def _store_otp(db, role: str, actor_id, email: str, code: str):
    with db.cursor() as cur:
        cur.execute(
            "UPDATE login_otps SET consumed = TRUE WHERE role = %s AND actor_id IS NOT DISTINCT FROM %s AND consumed = FALSE;",
            (role, actor_id),
        )
        cur.execute(
            "INSERT INTO login_otps (role, actor_id, email, code_hash, expires_at) VALUES (%s, %s, %s, %s, NOW() + INTERVAL '10 minutes');",
            (role, actor_id, email, auth_service.hash_otp(code)),
        )
        db.commit()


def _smtp_cfg() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", ""),
        "port": int(os.getenv("SMTP_PORT", "587") or 587),
        "user": os.getenv("SMTP_USER", ""),
        "pass": os.getenv("SMTP_PASS", ""),
        "from": os.getenv("SMTP_FROM", "hello@bizstackperks.com"),
        "name": os.getenv("SMTP_NAME", company()["name"]),
        "tls": os.getenv("SMTP_TLS", "true").lower(),
    }


async def _deliver_otp(email: str, code: str, actor: dict):
    cfg = _smtp_cfg()
    if not cfg["from"]:
        print(f"[OTP] Email not configured — code for {email}: {code}")
        return False, "Email delivery is not configured yet."
    body = (
        f"Hi {actor.get('name') or 'there'},\n\n"
        f"Your {cfg['name']} verification code is:\n\n    {code}\n\n"
        f"It expires in 10 minutes. If you didn't try to sign in, you can ignore this email.\n\n"
        f"— {cfg['name']}"
    )
    try:
        import documents_service

        await documents_service.send_email(
            {"SMTP_FROM": cfg["from"], "SMTP_NAME": cfg["name"]},
            email,
            f"Your {cfg['name']} login code",
            body,
        )
        return True, ""
    except Exception as e:
        return False, f"Could not send email: {e}"


def _finish_login(actor: dict, request: Request | None = None) -> RedirectResponse:
    token = auth_service.issue_session(actor["role"], actor.get("id"), actor.get("email"), actor.get("name"))
    resp = RedirectResponse(url=_login_redirect(actor["role"]), status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(key=auth_service.SESSION_COOKIE, value=token, httponly=True, samesite="lax", secure=_secure_cookies())
    resp.set_cookie(key="user_email", value=actor.get("email") or "", httponly=True, samesite="lax")
    resp.set_cookie(key="user_name", value=actor.get("name") or "", httponly=True, samesite="lax")
    resp.delete_cookie(auth_service.OTP_COOKIE)
    return resp


def _mask_email(email: str) -> str:
    if not email or "@" not in email:
        return email or ""
    name, domain = email.split("@", 1)
    shown = (name[:1] + "*") if len(name) <= 2 else (name[0] + "*" * (len(name) - 2) + name[-1])
    return f"{shown}@{domain}"


# --- AI tool handlers -------------------------------------------------------
def build_tool_handlers(db, stripe_svc):
    """DB/Stripe tool handlers that let the AI agent capture and manage leads."""

    def register_lead(name, phone, email="", project_type="", address="", budget="", timeline="", notes=""):
        # Same intake path as the website: the AI assistant is just another
        # source, and the queue state comes from the same rules.
        res = lead_intake.intake_lead(
            db, company="construction", source="ai-assistant", name=name,
            phone=phone, email=email, address=address, description=notes,
            project_type=project_type,
            extra={"budget": budget, "timeline": timeline},
        )
        return {"ok": True, "lead_id": res["lead_id"],
                "message": f"Lead saved for {name}."}

    def lookup_leads(phone):
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        with db.cursor() as cur:
            cur.execute("SELECT id, name, project_type, status, deposit_status, created_at FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 200;")
            rows = cur.fetchall()
        matches = [r for r in rows if digits and digits[-10:] in "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())]
        if not matches:
            matches = [r for r in rows if digits and digits[-10:] in "".join(ch for ch in str(r.get("name") or "") if ch.isdigit())]
        out = [
            {
                "id": r["id"], "name": r["name"], "project_type": r["project_type"],
                "status": r["status"], "deposit_status": r["deposit_status"],
                "created_at": r["created_at"].isoformat() if r.get("created_at") else "",
            }
            for r in matches
        ]
        return {"ok": True, "phone": phone, "leads": out}

    def get_business_summary():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'construction';")
            total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ('completed','lost') AND company = 'construction';")
            open_leads = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments WHERE company = 'construction';")
            pay = cur.fetchone()
        return {
            "ok": True,
            "total_leads": total,
            "open_leads": open_leads,
            "deposits_collected": pay["c"],
            "deposit_amount_cents": pay["amt"],
        }

    def list_leads(status=""):
        with db.cursor() as cur:
            if status:
                cur.execute(
                    "SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE status = %s AND company = 'construction' ORDER BY created_at DESC LIMIT 100;",
                    (status,),
                )
            else:
                cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "leads": [
            {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}} for r in rows
        ]}

    def update_lead_status(lead_id, status):
        if status not in LEAD_STATUSES:
            return {"ok": False, "error": f"Unknown status: {status}"}
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (status, lead_id))
            db.commit()
        return {"ok": True, "lead_id": lead_id, "status": status}

    def send_sms_message(to, body):
        ok = signalwire.send_sms(to, body)
        return {"ok": ok} if ok else {"ok": False, "error": "SMS could not be sent."}

    def send_email_message(to, subject, body):
        import documents_service

        # Do NOT gate on smtp_configured(): this deployment runs
        # EMAIL_TRANSPORT=gmail, so SMTP_HOST is legitimately unset while the
        # Gmail API path works fine. That guard rejected every send with
        # "SMTP not configured." even though the real transport was available.
        cfg = documents_service.smtp_config_from_env()
        if not (cfg.get("SMTP_FROM") or "").strip():
            return {"ok": False, "error": "No sending address is configured (SMTP_FROM is empty)."}

        # send_email returns False on failure and never raises, so the result has
        # to be checked. Discarding it logged the message to comms_logs and told
        # the owner "sent" for mail that was never delivered -- the same phantom
        # rows that had to be purged from comms_logs before.
        try:
            delivered = auto_reply.run_coro(documents_service.send_email(cfg, to, subject, body))
        except Exception as e:
            return {"ok": False, "error": f"Send failed: {e}"}

        if delivered is not True:
            return {
                "ok": False,
                "error": (
f"The mail transport did not accept the message. This usually means the "
                f"Gmail OAuth token is missing or expired for {cfg.get('SMTP_FROM') or 'the sending address'}. "
                f"Nothing was sent and nothing was logged."
                ),
            }

        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                "VALUES ('outbound', 'email', %s, %s, %s);",
                (cfg.get("SMTP_FROM"), to, body),
            )
            db.commit()
        return {"ok": True, "sent_to": to}

    def make_outbound_call(to, notes=""):
        digits = "".join(ch for ch in str(to or "") if ch.isdigit())
        if len(digits) < 10:
            return {"ok": False, "error": "I need a valid 10-digit phone number."}
        e164 = ("+1" + digits) if not digits.startswith("1") else ("+" + digits)
        if not vapi.is_configured():
            return {"ok": False, "error": "Vapi is not configured (VAPI_API_KEY missing)."}
        try:
            sid = vapi.create_ai_outbound_call(e164, notes or "")
        except Exception as e:
            return {"ok": False, "error": f"Call could not be placed: {e}"}
        if not sid:
            return {"ok": False, "error": "Call could not be placed right now."}
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'voice', 'assistant', %s, %s);",
                    (e164, f"[AI outbound call] {str(notes or '')[:500]}"),
                )
                db.commit()
        except Exception:
            pass
        return {"ok": True, "call_sid": sid, "to": e164}

    def create_deposit_link(lead_id):
        with db.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
            lead = cur.fetchone()
        if not lead:
            return {"ok": False, "error": "Lead not found."}
        try:
            url = stripe_svc.create_deposit_session(
                lead_id=lead["id"],
                customer_name=lead["name"],
                customer_email=lead.get("email") or "",
                project_type=lead.get("project_type") or "",
                amount_cents=lead.get("deposit_cents") or 0,
                base_url=f"https://{company()['domain']}",
            )
            return {"ok": True, "url": url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def create_deposit_links(limit: int = 10):
        """Batch: Stripe deposit checkout links for quoted leads awaiting one.

        One call replaces a create_deposit_link-per-lead loop (which the
        per-request tool budget would cut in half). Creates checkout sessions
        and marks them 'pending' -- the same order the UI route uses: session
        FIRST, then the UPDATE, because persisting 'pending' before Stripe
        answered once left 24 rows that could never complete. A failure leaves
        the lead untouched.

        It does NOT send anything to customers; the owner delivers each URL
        from the leads page. Leads already pending or paid are skipped, so
        re-running creates no duplicates.
        """
        try:
            n = max(1, min(int(limit), 25))
        except (TypeError, ValueError):
            n = 10
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, email, project_type, deposit_cents FROM leads "
                "WHERE LOWER(COALESCE(status, '')) = 'quoted' "
                "  AND COALESCE(deposit_status, 'none') = 'none' "
                "ORDER BY created_at DESC LIMIT %s;",
                (n,),
            )
            rows = cur.fetchall()
        if not rows:
            return {"ok": True, "created": [], "count": 0,
                    "note": "No quoted leads are waiting on a deposit link."}
        created, failed = [], []
        for lead in rows:
            amount_cents = int(lead.get("deposit_cents") or default_deposit_cents())
            try:
                checkout = stripe_svc.create_deposit_checkout(
                    lead_id=lead["id"],
                    customer_name=lead["name"],
                    customer_email=lead.get("email") or "",
                    project_type=lead.get("project_type") or "",
                    amount_cents=amount_cents,
                    base_url=f"https://{company()['domain']}",
                )
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET deposit_status = 'pending', deposit_cents = %s, "
                        "stripe_session_id = %s WHERE id = %s;",
                        (amount_cents, checkout["session_id"], lead["id"]),
                    )
                db.commit()
                created.append({"lead_id": lead["id"], "name": lead["name"],
                                "url": checkout["url"], "amount_cents": amount_cents})
            except Exception as e:
                try:
                    db.rollback()
                except Exception:
                    pass
                failed.append({"lead_id": lead["id"], "name": lead["name"],
                               "error": str(e)})
        return {"ok": not failed, "created": created, "failed": failed,
                "count": len(created),
                "note": "Links were created, not sent. The owner delivers each URL; "
                        "nothing was emailed to a customer."}

    # --- Extended owner-co-pilot toolkit -----------------------------------
    def lookup_permits(city="", address=""):
        with db.cursor() as cur:
            if address.strip():
                cur.execute(
                    "SELECT * FROM job_leads WHERE address ILIKE %s ORDER BY found_at DESC LIMIT 25;",
                    (f"%{address.strip()}%",),
                )
            elif city.strip():
                cur.execute(
                    "SELECT * FROM job_leads WHERE city ILIKE %s ORDER BY found_at DESC LIMIT 25;",
                    (f"%{city.strip()}%",),
                )
            else:
                cur.execute("SELECT * FROM job_leads ORDER BY found_at DESC LIMIT 15;")
            rows = cur.fetchall()
        return {"ok": True, "permits": [
            {
                "id": r["id"], "permit_number": r.get("permit_number"),
                "address": r.get("address"), "city": r.get("city"),
                "state": r.get("state"), "work_type": r.get("work_type"),
                "status": r.get("status"), "is_demo": r.get("is_demo"),
                "issue_date": r.get("issue_date"),
                "found_at": (r.get("found_at").isoformat() if r.get("found_at") else ""),
            }
            for r in rows
        ]}

    def enrich_permit_lead(email=""):
        """Resolve a permit lead from its con-permit-<digest>@lead.local tracking email.

        Read-only: one SELECT, no writes, no outbound contact. Copilot/owner tier.
        """
        result = permit_enricher.enrich((email or "").strip())
        if result.get("status") != "success":
            return {"ok": False, "error": result.get("message", "unresolved")}
        return {"ok": True, **result}

    def list_crew():
        with db.cursor() as cur:
            cur.execute(
                "SELECT id, name, phone, email, role, pay_type, pay_rate, is_active, "
                "stripe_account_id, bank_status FROM crew ORDER BY is_active DESC, name;"
            )
            rows = cur.fetchall()
        return {"ok": True, "crew": [
            {**{k: (v.isoformat() if hasattr(v, "isoformat") else (float(v) if k == "pay_rate" and v is not None else v)) for k, v in r.items()}}
            for r in rows
        ]}

    def lookup_crew_timesheets(crew_id=0, project_id=0, status=""):
        with db.cursor() as cur:
            if crew_id:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.crew_id = %s ORDER BY t.work_date DESC LIMIT 100;", (crew_id,))
            elif project_id:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.project_id = %s ORDER BY t.work_date DESC LIMIT 100;", (project_id,))
            elif status:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE t.status = %s ORDER BY t.work_date DESC LIMIT 100;", (status,))
            else:
                cur.execute(
                    "SELECT t.*, c.name AS crew_name, p.name AS project_name "
                    "FROM timesheets t JOIN crew c ON c.id = t.crew_id "
                    "LEFT JOIN projects p ON p.id = t.project_id "
                    "ORDER BY t.work_date DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "timesheets": [
            {
                "id": r["id"], "crew_id": r["crew_id"], "crew_name": r.get("crew_name"),
                "project_id": r.get("project_id"), "project_name": r.get("project_name"),
                "work_date": r.get("work_date").isoformat() if r.get("work_date") else "",
                "hours": float(r.get("hours") or 0), "work_type": r.get("work_type"),
                "status": r.get("status"), "notes": r.get("notes"),
            }
            for r in rows
        ]}

    def get_payroll_summary():
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payroll_runs WHERE status = 'open' ORDER BY period_start DESC LIMIT 1;")
            run = cur.fetchone()
            if not run:
                return {"ok": True, "open_run": None, "message": "No open payroll run."}
            cur.execute(
                "SELECT pl.*, c.name AS crew_name, c.bank_status, c.stripe_account_id "
                "FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id "
                "WHERE pl.run_id = %s ORDER BY c.name;", (run["id"],))
            lines = cur.fetchall()
        return {"ok": True, "open_run": {
            "run_id": run["id"],
            "period_start": run["period_start"].isoformat(),
            "period_end": run["period_end"].isoformat(),
        }, "lines": [
            {
                "crew_id": l["crew_id"], "crew_name": l["crew_name"],
                "hours": float(l.get("hours") or 0), "overtime_hours": float(l.get("overtime_hours") or 0),
                "gross_cents": l["gross_cents"],
                "bank_status": l.get("bank_status"), "direct_deposit_ready": bool(l.get("stripe_account_id")),
            }
            for l in lines
        ]}

    def run_payroll():
        with db.cursor() as cur:
            cur.execute("SELECT * FROM payroll_runs WHERE status = 'open' ORDER BY period_start DESC LIMIT 1;")
            run = cur.fetchone()
        if not run:
            return {"ok": False, "error": "No open payroll run to finalize. Create one on the Payroll page first."}
        run_id = run["id"]
        with db.cursor() as cur:
            cur.execute(
                "SELECT pl.*, c.name AS crew_name, c.pay_rate, c.stripe_account_id, c.bank_status "
                "FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id WHERE pl.run_id = %s;", (run_id,))
            lines = cur.fetchall()
        paid, skipped = [], []
        for l in lines:
            acct = (l.get("stripe_account_id") or "").strip()
            if not acct:
                skipped.append({"crew_name": l.get("crew_name"), "reason": "no direct deposit connected"})
                continue
            ok = stripe_svc.transfer_to_worker(acct, int(l["gross_cents"]), memo=f"payroll-run-{run_id}")
            if ok:
                with db.cursor() as cur:
                    cur.execute(
                        "INSERT INTO payments (lead_id, stripe_payment_intent_id, amount_cents, currency, status, company) "
                        "VALUES (NULL, %s, %s, 'usd', 'paid', 'construction');",
                        (f"payroll-run-{run_id}-crew-{l['crew_id']}", l["gross_cents"]),
                    )
                paid.append({"crew_name": l.get("crew_name"), "gross_cents": l["gross_cents"]})
            else:
                skipped.append({"crew_name": l.get("crew_name"), "reason": "Stripe transfer failed"})
        with db.cursor() as cur:
            cur.execute("UPDATE payroll_runs SET status = 'closed' WHERE id = %s;", (run_id,))
            db.commit()
        # Only the crew who actually received money are relieved. A skipped
        # transfer leaves the wage owed, which is correct -- the liability was
        # accrued when the run was created and only goes away when cash moves.
        if paid:
            try:
                ledger_sync.ensure_schema(db)
                ledger_sync.sync_payroll_paid(db, run_id, company=_BID_COMPANY)
            except Exception as e:
                logger.warning("ledger not updated for payroll run %s: %s", run_id, e)
        return {
            "ok": True, "run_id": run_id,
            "paid": paid,
            "not_paid": skipped,
            "total_paid_cents": sum(p["gross_cents"] for p in paid),
        }

    def get_accounting_summary():
        with db.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(SUM(amount_cents),0) AS amt, COUNT(*) AS n "
                "FROM payments WHERE company = 'construction' AND status = 'paid';")
            collected = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(deposit_cents),0) AS amt, COUNT(*) AS n "
                "FROM leads WHERE company = 'construction' AND deposit_status = 'paid';")
            deposits = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(gross_cents),0) AS amt FROM payroll_lines pl "
                "JOIN payroll_runs pr ON pr.id = pl.run_id WHERE pr.status = 'closed';")
            payroll_paid = cur.fetchone()
            cur.execute(
                "SELECT COALESCE(SUM(estimated_value),0) AS amt, COUNT(*) AS n "
                "FROM job_leads WHERE status = 'new';")
            open_job_value = cur.fetchone()
        return {
            "ok": True,
            "collected_deposits_cents": deposits["amt"],
            "collected_deposits_count": deposits["n"],
            "total_collections_cents": collected["amt"],
            "payments_count": collected["n"],
            "payroll_paid_total_cents": payroll_paid["amt"],
            "open_job_feed_value_cents": open_job_value["amt"],
            "open_job_feed_count": open_job_value["n"],
        }

    def estimate_materials(project_type="", sqft=0, include=None, live=False):
        svc = materials_service.BusinessMaterialsService()
        return svc.estimate_materials(project_type=project_type, sqft=sqft, include=include, live=bool(live))

    def get_material_price(sku):
        svc = materials_service.BusinessMaterialsService()
        cents = svc.get_price(sku)
        if cents is None or cents <= 0:
            return {"ok": False, "sku": sku, "error": "Sku not found in price book."}
        return {"ok": True, "sku": sku, "price_cents": int(cents), "price_dollars": round(cents / 100, 2)}

    def search_materials(category="", brand="", query="", store="", limit=8):
        svc = materials_service.BusinessMaterialsService()
        try:
            return svc.search_materials(category=category, brand=brand, query=query, store=store, limit=limit)
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def run_site_health_check():
        site = f"https://{company()['domain']}"
        checks = {"construction_site": True, "sms": True, "stripe": True, "database": True}
        try:
            with db.cursor() as cur:
                cur.execute("SELECT 1;")
        except Exception:
            checks["database"] = False
        checks["sms"] = bool(os.getenv("SIGNALWIRE_PROJECT_ID") or os.getenv("SIGNALWIRE_API_TOKEN"))
        checks["stripe"] = stripe_svc.is_configured()
        return {
            "ok": True,
            "sites": {
                "construction": f"{site} ✓",
            },
            "checks": checks,
        }

    def generate_training_deck(kind="worker"):
        kind = training_service.CONSTRUCTION_DECK_KINDS.get(kind, kind)
        if kind not in ("worker", "construction",
                        "construction-trades", "construction-safety",
                        "construction-app", "construction-ethics"):
            return {"ok": False, "error": f"Unknown deck kind: {kind}"}
        try:
            data = training_service.build_deck(kind)
        except Exception as e:
            return {"ok": False, "error": f"Could not build deck: {e}"}
        DECK_LABELS = {
            "construction": "Crew Orientation + OSHA-10 Baseline",
            "construction-trades": "Tools of the Trade",
            "construction-safety": "Job Site Safety (OSHA-10 Baseline)",
            "construction-app": "Crew App — Install & Time Reporting",
            "construction-ethics": "Workplace Ethics & Professional Conduct",
            "worker": "Worker Orientation",
        }
        label = DECK_LABELS.get(kind, kind)
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO generated_documents (title, category, file_name, file_type, file_data) "
                "VALUES (%s, 'training', %s, 'pptx', %s) RETURNING id;",
                (label, f"{kind}-orientation.pptx", data),
            )
            doc_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "doc_id": doc_id, "title": label,
                "download_url": f"/docs/download/{doc_id}"}

    def grade_training_quiz(crew_id, answers, test_slug="osha"):
        test = training_service.TRAINING_BY_SLUG.get(test_slug)
        if not test:
            return {"ok": False, "error": f"Unknown test slug: {test_slug}"}
        try:
            grade = training_service.grade_training(answers, test["questions"])
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        name = ""
        if crew_id:
            with db.cursor() as cur:
                cur.execute("SELECT name FROM crew WHERE id = %s;", (crew_id,))
                row = cur.fetchone()
                if row:
                    name = row["name"]
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_quiz_results (worker_id, worker_name, score, total, passed, answers_json, test_slug, test_title) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;",
                (crew_id, name or "Crew (copilot)", grade["correct"], grade["total"], grade["passed"],
                 json.dumps(answers), test["slug"], test["title"]),
            )
            result_id = cur.fetchone()["id"]
            db.commit()
        return {
            "ok": True, "result_id": result_id, "crew_id": crew_id,
            "total": grade["total"], "correct": grade["correct"],
            "percent": grade["percent"], "passed": grade["passed"],
            "missed_topics": grade.get("missed_topics", []),
            "message": "Passed — clear for jobs." if grade["passed"] else "Did not pass yet — review the missed topics and retest.",
        }

    def training_status():
        """Per-crew training report: which OSHA-10/safety + trade tests each crew member has passed."""
        with db.cursor() as cur:
            cur.execute(
                "SELECT Q.worker_id, c.name, c.is_active, Q.test_slug, Q.test_title, MAX(Q.q) AS best "
                "FROM ("
                "  SELECT worker_id, test_slug, test_title, "
                "         ROUND(100.0 * score / NULLIF(total, 0)) AS q "
                "  FROM worker_quiz_results WHERE passed = TRUE"
                ") Q JOIN crew c ON c.id = Q.worker_id "
                "GROUP BY Q.worker_id, c.name, c.is_active, Q.test_slug, Q.test_title "
                "ORDER BY c.is_active DESC, c.name, Q.test_title;"
            )
            passed_rows = cur.fetchall()
            cur.execute("SELECT id, name, phone, role, is_active FROM crew ORDER BY is_active DESC, name;")
            crew = cur.fetchall()
        by_crew = {}
        for r in passed_rows:
            by_crew.setdefault(r["worker_id"], []).append({
                "test_slug": r["test_slug"] or "osha",
                "test_title": r["test_title"] or "Site Safety Orientation (OSHA-10 baseline)",
                "best_percent": int(r["best"]),
            })
        out = []
        for c in crew:
            done = by_crew.get(c["id"], [])
            out.append({
                "crew_id": c["id"], "name": c["name"], "phone": c["phone"],
                "role": c["role"], "is_active": c["is_active"],
                "tests_passed": done,
                "all_done": any(t["test_slug"] == "osha" for t in done),
            })
        outstanding = [r for r in out if not r["all_done"]]
        return {"ok": True, "crew": out, "outstanding_count": len(outstanding),
                "outstanding": outstanding}

    def remind_crew_training(crew_id=0):
        """Text a reminder to crew who haven't passed the OSHA-10 baseline safety orientation yet."""
        if crew_id:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT c.id, c.name, c.phone, MAX(q.worker_id) IS NOT NULL AS did "
                    "FROM crew c LEFT JOIN worker_quiz_results q "
                    "  ON q.worker_id = c.id AND q.test_slug = 'osha' AND q.passed = TRUE "
                    "WHERE c.id = %s GROUP BY c.id, c.name, c.phone;", (crew_id,))
                rows = cur.fetchall()
        else:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT c.id, c.name, c.phone, MAX(q.worker_id) IS NOT NULL AS did "
                    "FROM crew c LEFT JOIN worker_quiz_results q "
                    "  ON q.worker_id = c.id AND q.test_slug = 'osha' AND q.passed = TRUE "
                    "WHERE c.is_active = TRUE GROUP BY c.id, c.name, c.phone "
                    "HAVING MAX(q.worker_id) IS NULL;")
                rows = cur.fetchall()
        domain = company()["domain"]
        sent, reminded = [], 0
        for r in rows:
            if r["did"]:
                continue
            digits = "".join(ch for ch in str(r["phone"] or "") if ch.isdigit())
            if len(digits) < 10:
                continue
            e164 = "+" + digits if not digits.startswith("+") else digits
            body = (f"Hi {r['name']} — time to knock out your Buildstack safety orientation "
                    f"(about 10 minutes, covers the OSHA-10 basics + your trade). "
                    f"Open {domain}/app -> Training tab, read the slides, then pass the quiz. "
                    f"Passing it clears you for job sites. Questions? Text the office.")
            if signalwire.send_sms(e164, body):
                reminded += 1
            sent.append({"crew_id": r["id"], "name": r["name"]})
        return {"ok": True, "reminded_count": reminded, "reminded": sent}

    def lookup_property(address=""):
        """Look up a US property by street address (RentCast) for real sqft/beds/baths."""
        a = str(address or "").strip()
        if not a:
            return {"ok": False, "error": "What's the property street address?"}
        prop = property_service.lookup_address(a)
        if not prop:
            return {
                "ok": False,
                "configured": property_service.is_configured(),
                "error": "I couldn't pull that property right now.",
            }
        out = {k: prop.get(k) for k in ("address", "sqft", "beds", "baths", "year_built", "stories", "property_type", "source")}
        out["ok"] = True
        return out

    def quote_project(project_type="", address="", sqft=0, materials="", name="", phone="", notes="", lead_id=0):
        """Real ballpark quote for any construction trade, grounded in the property + material grades."""
        if not str(project_type or "").strip():
            return {"ok": False, "error": "What type of work do you need — roof, kitchen, bath, whole-home?"}
        prop = None
        manual = 0.0
        try:
            manual = max(float(sqft or 0), 0)
        except (TypeError, ValueError):
            manual = 0.0
        if manual <= 0 and address:
            prop = property_service.lookup_address(address)
        est = estimating_service.estimate(project_type, prop, manual if manual > 0 else None, str(materials or ""))
        if not est:
            est = estimating_service.auto_quote(project_type, manual if manual > 0 else None)
        if not est:
            return {"ok": False, "error": "I don't have a pricing model for that kind of work yet — the estimator will follow up."}
        assumed = bool(est.get("assumed_sqft")) or (not prop and manual <= 0 and est.get("quantifier") and "sq ft" in est.get("quantifier", "").lower())
        needs_sqft = assumed and manual <= 0
        out = {
            "ok": True,
            "project_type": project_type,
            "label": est.get("label"),
            "quantifier": est.get("quantifier"),
            "low_est": est.get("low_est"),
            "high_est": est.get("high_est"),
            "sqft": est.get("sqft"),
            "material_notes": est.get("material_notes") or [],
            "notes": est.get("notes") or [],
            "assumed_sqft": assumed,
            "needs_sqft": needs_sqft,
            "property": ({"address": prop.get("address"), "sqft": prop.get("sqft"), "beds": prop.get("beds"), "baths": prop.get("baths"), "year_built": prop.get("year_built")} if prop else None),
        }
        stamped_lead = None
        if name and phone:
            lead_result = register_lead(name=name, phone=phone, project_type=project_type,
                                        address=address, notes=(f"Quoted {est.get('low_est')}-{est.get('high_est')}: {est.get('material_notes')}; {notes}"))
            stamped_lead = lead_result.get("lead_id")
        elif lead_id:
            try:
                with db.cursor() as cur:
                    cur.execute("UPDATE leads SET estimate_low_cents = %s, estimate_high_cents = %s, "
                                "status = COALESCE(NULLIF(status,''),'quoted') WHERE id = %s RETURNING id;",
                                (est.get("low_cents"), est.get("high_cents"), int(lead_id)))
                    row = cur.fetchone()
                    db.commit()
                stamped_lead = row["id"] if row else int(lead_id)
            except Exception:
                db.rollback()
                stamped_lead = int(lead_id)
        out["lead_id"] = stamped_lead
        return out

    def get_inventory(company="construction", category="", low_only=False):
        """List supply inventory for construction, optionally at/below reorder level."""
        co = "construction"
        with db.cursor() as cur:
            cur.execute("SELECT * FROM supplies_inventory WHERE company = %s ORDER BY category, item_name;", (co,))
            rows = cur.fetchall()
        items = []
        for r in rows:
            if category and (r.get("category") or "").lower() != str(category).lower():
                continue
            qty = float(r.get("qty_on_hand") or 0)
            reorder = float(r.get("reorder_level") or 0)
            low = qty <= reorder
            if low_only and not low:
                continue
            items.append({"id": r["id"], "item_name": r["item_name"], "sku": r.get("sku"),
                          "category": r.get("category"), "qty_on_hand": qty, "unit": r.get("unit"),
                          "reorder_level": reorder, "low": low, "notes": r.get("notes")})
        return {"ok": True, "company": co, "items": items}

    def update_inventory(item_name="", qty_delta=0, company="construction", category="", set_qty=None):
        """Adjust supply quantity (add/remove via qty_delta, or set absolute)."""
        if not str(item_name or "").strip():
            return {"ok": False, "error": "Which supply item?"}
        co = "construction"
        with db.cursor() as cur:
            cur.execute("SELECT * FROM supplies_inventory WHERE company = %s AND item_name ILIKE %s;",
                        (co, str(item_name).strip()))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO supplies_inventory (company, item_name, category, qty_on_hand, unit, reorder_level) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *;",
                    (co, str(item_name).strip(), category or "general", 0, "each", 0))
                row = cur.fetchone()
            cur_qty = float(row.get("qty_on_hand") or 0)
            reorder = float(row.get("reorder_level") or 0)
            if set_qty is not None:
                new_qty = float(set_qty)
            else:
                new_qty = cur_qty + float(qty_delta or 0)
            cur.execute("UPDATE supplies_inventory SET qty_on_hand = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s;",
                        (new_qty, row["id"]))
            db.commit()
        return {"ok": True, "id": row["id"], "item": row["item_name"], "qty_on_hand": new_qty,
                "low": new_qty <= reorder, "company": co}

    def reorder_report(company="construction"):
        """Every supply at/below its reorder level — the restock shopping list."""
        co = "construction"
        with db.cursor() as cur:
            cur.execute("SELECT * FROM supplies_inventory WHERE company = %s "
                        "AND qty_on_hand <= reorder_level ORDER BY category, item_name;", (co,))
            rows = cur.fetchall()
        items = [{"item_name": r["item_name"], "category": r.get("category"),
                  "qty_on_hand": float(r.get("qty_on_hand") or 0), "unit": r.get("unit"),
                  "reorder_level": float(r.get("reorder_level") or 0)} for r in rows]
        return {"ok": True, "company": co, "items": items}

    def find_people(query="", company="construction"):
        """Search employees (crew), customers, and leads by name/phone/email."""
        q = str(query or "").strip()
        if not q:
            return {"ok": False, "error": "Give me a name or number to look up."}
        like = lambda col: f"{col} ILIKE %s"
        matches = []
        with db.cursor() as cur:
            cur.execute(f"SELECT id, name, phone, email, role, 'worker' AS kind FROM crew WHERE {like('name')} OR {like('phone')} OR {like('email')} LIMIT 20;",
                        (f"%{q}%", f"%{q}%", f"%{q}%"))
            for r in cur.fetchall():
                r["company"] = "construction"
                matches.append(r)
            try:
                cur.execute(f"SELECT id, name, phone, email, 'customer' AS kind FROM customers WHERE {like('name')} OR {like('phone')} OR {like('email')} LIMIT 20;",
                            (f"%{q}%", f"%{q}%", f"%{q}%"))
                for r in cur.fetchall():
                    r["company"] = "construction"
                    matches.append(r)
            except Exception:
                pass
            lead_vis = source_visibility_clause()
            lead_vis_params = source_visibility_params()
            cur.execute(f"SELECT id, name, phone, email, project_type, 'lead' AS kind FROM leads "
                        f"WHERE ({like('name')} OR {like('phone')} OR {like('email')}){lead_vis} LIMIT 20;",
                        (f"%{q}%", f"%{q}%", f"%{q}%", *lead_vis_params))
            for r in cur.fetchall():
                r["company"] = r.get("company") or "construction"
                matches.append(r)
        if not matches:
            return {"ok": False, "error": f"No employee, customer, or lead matching '{q}'."}
        return {"ok": True, "people": matches[:25]}

    def navigation_guide(role="", company="construction"):
        """How to navigate our websites/apps for employees, customers, and the owner."""
        role = (role or "").lower().strip()
        if "work" in role or "employ" in role or "crew" in role or "staff" in role or "team" in role:
            role = "employee"
        elif "own" in role or "admin" in role or "boss" in role or "shaun" in role:
            role = "owner"
        elif "customer" in role or "client" in role or "caller" in role or "prospect" in role:
            role = "customer"
        else:
            role = "anyone"
        guide = {
            "employee": [
                "Construction site: construction.bizstackperks.com — crew tools at /construction/crew (roles, timesheets, schedule, training) and your app at /construction/app (Training, jobs, clock in/out).",
                "Text the office any time at +1 (757) 908-7121.",
            ],
            "customer": [
                "Construction site: construction.bizstackperks.com — free-estimate form at /quote and the instant-quote tool that ballparks a range in minutes from an address.",
                "Call or text the same number any time: +1 (757) 908-7121.",
            ],
            "owner": [
                "Construction dashboard: construction.bizstackperks.com — project pipeline, deposits, crew, accounting, permits, radar at /dashboard, /leads, /payments.",
            ],
            "anyone": [
                "Construction: construction.bizstackperks.com — instant-quote from an address, free-estimate form at /quote, portfolio, crew app.",
                "Call or text the same number any time: +1 (757) 908-7121.",
            ],
        }
        if company:
            if str(company).lower() in ("construction", "buildstack"):
                return {"ok": True, "role": role, "pages": [g for g in guide.get(role, guide["anyone"]) if "construction" in g.lower() or "owner" in g.lower()] or guide["owner"]}
            return {"ok": True, "role": role, "pages": [g for g in guide.get(role, guide["anyone"]) if "construction" not in g.lower()] or guide["owner"]}
        return {"ok": True, "role": role, "pages": guide.get(role, guide["anyone"])}

    return {
        "register_lead": register_lead,
        "lookup_leads": lookup_leads,
        "get_business_summary": get_business_summary,
        "list_leads": list_leads,
        "update_lead_status": update_lead_status,
        "send_sms_message": send_sms_message,
        "send_email_message": send_email_message,
        "create_deposit_link": create_deposit_link,
        "create_deposit_links": create_deposit_links,
        "lookup_permits": lookup_permits,
        "enrich_permit_lead": enrich_permit_lead,
        "list_crew": list_crew,
        "lookup_crew_timesheets": lookup_crew_timesheets,
        "get_payroll_summary": get_payroll_summary,
        "run_payroll": run_payroll,
        "get_accounting_summary": get_accounting_summary,
        "estimate_materials": estimate_materials,
        "get_material_price": get_material_price,
        "search_materials": search_materials,
        "make_outbound_call": make_outbound_call,
        "run_site_health_check": run_site_health_check,
        "generate_training_deck": generate_training_deck,
        "grade_training_quiz": grade_training_quiz,
        "training_status": training_status,
        "remind_crew_training": remind_crew_training,
        "lookup_property": lookup_property,
        "quote_project": quote_project,
        "get_inventory": get_inventory,
        "update_inventory": update_inventory,
        "reorder_report": reorder_report,
        "find_people": find_people,
        "navigation_guide": navigation_guide,
    }


def build_copilot_handlers(db, owner_email: str):
    """Everything `build_tool_handlers` offers, plus the operator-only tier.

    Copilot-only, never handed to the public widget: calculate, search_comms,
    maps, calendar, and the durable task list. `search_comms` is how Copilot
    reads the 24/7 phone assistant's calls and texts -- straight from
    comms_logs, rather than by having a conversation with the voice agent.

    send_email_message is DELIBERATELY WITHHELD from the Copilot as of
    2026-10-03. It was the one unrestricted send path in the build: no rate
    limit, no per-recipient dedupe, no allowlist, and no require-confirmation.
    Asked to research property owners, the Copilot looped on it and sent three
    identical emails within ~30 seconds to real@norfolk.gov and
    real.estate@norfolk.gov asking a public office to hand over the personal
    ownership records of specific homeowners. Those are real sent messages and
    cannot be recalled.

    The handler still exists in build_tool_handlers and is untouched -- it is
    used by paths that already enforce contact_policy_allows. Restoring it here
    is a one-line change, but it should not come back without a cooldown, a
    dedupe check, and a recipient allowlist. Note that the handler's own
    docstring never claimed to be safe for arbitrary recipients; the mistake was
    treating "the owner asked for it" as sufficient authorization to send.

    What the Copilot DOES get is skip_trace_owner and save_contact, which
    complete the pipeline the owner needs without giving it a way to transmit:
    address -> owner name (cached), name -> email (its own bounded web_search),
    then persist the result onto the lead. Contacts used to be reported in chat
    and evaporate, so there was no lead row and no dedupe. Writing a contact is
    recoverable -- the owner sees a wrong address on the leads page -- whereas
    emailing one is not. That asymmetry is why these are here and the send tool
    is not.
    """
    handlers = build_tool_handlers(db, stripe_svc)
    handlers.pop("send_email_message", None)
    handlers.update(copilot_ops.build_comms_log_tools(db))
    handlers.update(copilot_ops.build_maps_tools(db))
    handlers.update(copilot_ops.build_calendar_tools(db))
    handlers.update(copilot_ops.build_task_tools(db, owner_email))
    handlers.update(copilot_ops.build_web_search_tools())
    handlers.update(copilot_ops.build_contact_tools(db, owner_email))
    handlers["calculate"] = copilot_ops.calculate
    return handlers


# --- Public marketing pages -------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


@app.get("/services", response_class=HTMLResponse)
async def services_page(request: Request):
    return templates.TemplateResponse(request=request, name="services.html", context={})


@app.get("/about", response_class=HTMLResponse)
async def about_page(request: Request):
    return templates.TemplateResponse(request=request, name="about.html", context={})


@app.get("/portfolio", response_class=HTMLResponse)
async def portfolio_page(request: Request):
    return templates.TemplateResponse(request=request, name="portfolio.html", context={})


@app.get("/quote", response_class=HTMLResponse)
async def quote_page(request: Request):
    return templates.TemplateResponse(request=request, name="quote.html", context={})


@app.get("/contact", response_class=HTMLResponse)
async def contact_page(request: Request):
    return templates.TemplateResponse(request=request, name="contact.html", context={})


@app.get("/legal", response_class=HTMLResponse)
async def legal_page(request: Request):
    return templates.TemplateResponse(request=request, name="legal.html", context={})


VISION_QUOTE_MAX_UPLOAD_BYTES = 12 * 1024 * 1024
VISION_QUOTE_CACHE_MAX = 200
VISION_QUOTE_WINDOW_SECONDS = 300
VISION_QUOTE_MAX_PER_WINDOW = 6
_vision_quote_cache: dict = {}
_vision_quote_calls: dict = {}


def _client_ip(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _vision_quote_throttled(ip: str) -> bool:
    now = time.time()
    calls = [t for t in _vision_quote_calls.get(ip, []) if now - t < VISION_QUOTE_WINDOW_SECONDS]
    if len(calls) >= VISION_QUOTE_MAX_PER_WINDOW:
        _vision_quote_calls[ip] = calls
        return True
    calls.append(now)
    _vision_quote_calls[ip] = calls
    if len(_vision_quote_calls) > 5000:
        for key in [k for k, v in _vision_quote_calls.items() if not v or now - v[-1] > VISION_QUOTE_WINDOW_SECONDS]:
            _vision_quote_calls.pop(key, None)
    return False


@app.post("/api/vision/quote", response_class=JSONResponse)
async def vision_quote_endpoint(
    request: Request,
    company: str = Form("construction"),
    sqft: float | None = Form(None),
    photo: UploadFile = File(...),
):
    """Public photo -> scope -> ballpark.

    Does not create a lead: this is an unauthenticated POST, so wiring it to
    lead creation should wait until reCAPTCHA gates the public forms. Throttled
    per IP and cached on the image fingerprint, since each analyze bills three
    Vision units and a resubmit loop would burn the quota.
    """
    import google_vision
    import vision_quote

    if not vision_quote.is_enabled():
        return JSONResponse(content={"available": False, "why": "photo quoting disabled"}, status_code=200)
    ip = _client_ip(request)
    if _vision_quote_throttled(ip):
        return JSONResponse(
            content={"available": False, "why": "too many photo quotes, try again shortly"},
            status_code=429,
        )
    data = await photo.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > VISION_QUOTE_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Photo too large (max 12 MB)")

    fingerprint = google_vision.image_fingerprint(data)
    cached = _vision_quote_cache.get(fingerprint)
    if cached is not None:
        return JSONResponse(content={**cached, "cached": True})

    vision = google_vision.annotate(data)
    if not vision:
        return JSONResponse(
            content={"available": False, "why": "image analysis unavailable, please send the photo to us directly"},
            status_code=200,
        )
    result = vision_quote.quote_from_vision(vision, sqft)
    result["vision"] = vision
    if len(_vision_quote_cache) >= VISION_QUOTE_CACHE_MAX:
        _vision_quote_cache.pop(next(iter(_vision_quote_cache)), None)
    _vision_quote_cache[fingerprint] = result
    return JSONResponse(content=result)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/maps/health", response_class=JSONResponse)
async def maps_health(request: Request):
    """Live check on Google Maps, for when the site looks fine but addresses
    will not resolve. Admin only: it makes a billable call and reports on the
    project's billing state."""
    require_admin(request)
    return JSONResponse(content={
        "config": google_maps.maps_status(),
        "tools_advertised": google_maps.maps_tools_available(),
        "probe": google_maps.live_probe(),
    })


@app.get("/robots.txt", response_class=Response)
async def robots_txt(request: Request):
    base = _site_base(request)
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /dashboard\n"
        "Disallow: /leads\n"
        "Disallow: /payments\n"
        "Disallow: /appearance\n"
        "Disallow: /copilot\n"
        "Disallow: /login\n"
        "\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(content=body, media_type="text/plain")


@app.get("/sitemap.xml", response_class=Response)
async def sitemap_xml(request: Request):
    base = _site_base(request)
    today = date.today().isoformat()
    pages = ["/", "/services", "/get-started", "/instant-quote", "/portfolio", "/finance",
             "/about", "/quote", "/contact", "/legal"]
    urls = "".join(
        f'  <url><loc>{base}{p}</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>{"1.0" if p == "/" else "0.8"}</priority></url>\n'
        for p in pages
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + urls
        + "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


# --- Lead capture -----------------------------------------------------------
def _is_form_bot(honeypot: str, source: str = "") -> bool:
    """A filled honeypot is a bot. Log it and let the caller fake success.

    Every public form here costs money on submit -- an owner email, an
    auto-reply to whatever address was typed, a Zoho CRM push, sometimes a
    RentCast call -- and none had any abuse gate. The field is visually
    hidden with tabindex=-1, so a person never fills it. Faking success
    rather than erroring keeps the bot from learning which field tripped it.
    """
    if (honeypot or "").strip():
        print(f"[spam] honeypot tripped on {source or 'form'}; dropped", flush=True)
        return True
    return False


@app.post("/submit-lead")
async def submit_lead(
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    project_type: str = Form(""),
    address: str = Form(""),
    budget: str = Form(""),
    timeline: str = Form(""),
    description: str = Form(""),
    source: str = Form("website"),
    company_website: str = Form(""),
    db=Depends(get_db),
):
    src = (source or "").strip().lower() or "website"
    if _is_form_bot(company_website, src):
        return JSONResponse(content={"status": "success", "lead_id": 0})
    res = lead_intake.intake_lead(
        db, company="construction", source=src, name=name, phone=phone,
        email=(email or "").strip(), address=address, description=description,
        project_type=project_type,
        extra={"budget": budget, "timeline": timeline},
    )
    lead_id = res["lead_id"]
    created = res["created"]
    if not created:
        # A duplicate or merged lead: nothing new to notify, draft, or schedule.
        return JSONResponse(content={"status": "success", "lead_id": lead_id, "duplicate": True})
    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=name, phone=phone, email=(email or "").strip(),
            service=(project_type or ""), address=address,
            budget=budget, timeline=timeline, message=description, source=src,
        )
    except Exception as e:
        print(f"⚠️ submit-lead owner notify failed: {e}", flush=True)
    try:
        auto_reply.auto_reply_to_lead(
            db, "construction", name=name, phone=phone, email=(email or "").strip(),
            service=(project_type or ""), address=address,
            budget=budget, timeline=timeline, message=description, source=src,
            lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ submit-lead auto-reply failed: {e}", flush=True)
    try:
        _follow_up_new_lead(lead_id)
    except Exception as e:
        print(f"⚠️ submit-lead follow-up failed: {e}", flush=True)
    # CASCADING PIPELINE: Lead created → auto-enrich → auto-contact → auto-quote
    try:
        await asyncio.to_thread(
            lead_pipeline.on_lead_created, db, lead_id
        )
        print(f"🔄 Lead {lead_id} cascade initiated", flush=True)
    except Exception as e:
        print(f"⚠️ submit-lead cascade failed: {e}", flush=True)
    await asyncio.to_thread(zoho_sync.after_intake, db, lead_id)
    return JSONResponse(content={"status": "success", "lead_id": lead_id})


def _run_followups() -> dict:
    """One sweep of the inbound follow-up schedule, with the app's own
    transports and caps. Called hourly by followups.start()."""
    conn = psycopg.connect(db_url, row_factory=dict_row)
    try:
        cfg = documents_service.smtp_config_from_env()

        def _send_email(to, subject, body):
            if not documents_service.smtp_configured(cfg):
                return False
            return bool(auto_reply.run_coro(documents_service.send_email(cfg, to, subject, body.replace("\n", "<br>"))))

        _send_sms = signalwire.send_sms if signalwire.is_configured() else None
        return followups.run_once(
            conn, send_email=_send_email, send_sms=_send_sms,
            channel_allowed=lambda ch: auto_reply._channel_allowed(conn, ch),
            review_mode=auto_reply._review_mode(),
        )
    finally:
        conn.close()


@app.get("/book/{token}", response_class=HTMLResponse)
async def book_walkthrough_page(token: str, request: Request, db=Depends(get_db)):
    """Self-serve walkthrough booking, reached from every follow-up message."""
    lead_id = followups.verify_booking_token(token)
    lead = followups.lead_for_booking(db, lead_id) if lead_id else None
    if not lead:
        return templates.TemplateResponse(request=request, name="book.html",
                                          context={"invalid": True, "token": token}, status_code=404)
    return templates.TemplateResponse(request=request, name="book.html", context={
        "lead": lead, "token": token, "slots": followups.slots(),
        "booked": lead.get("walkthrough_at"),
        "booked_label": followups.window_label(lead["walkthrough_at"]) if lead.get("walkthrough_at") else "",
        "saved": request.query_params.get("saved", ""), "error": request.query_params.get("error", ""),
    })


@app.post("/book/{token}")
async def book_walkthrough_submit(token: str, request: Request, slot: str = Form(""),
                                  note: str = Form(""), phone: str = Form(""), email: str = Form(""),
                                  company_website: str = Form(""), db=Depends(get_db)):
    lead_id = followups.verify_booking_token(token)
    if not lead_id or _is_form_bot(company_website, "booking"):
        return RedirectResponse(url=f"/book/{token}?saved=1", status_code=status.HTTP_303_SEE_OTHER)
    when = followups.parse_slot(slot)
    if not when:
        return RedirectResponse(url=f"/book/{token}?error=Pick+one+of+the+listed+times", status_code=status.HTTP_303_SEE_OTHER)
    lead = followups.book(db, lead_id, when, note=note, phone=phone, email=email)
    if not lead:
        return RedirectResponse(url=f"/book/{token}?error=We+could+not+find+your+request", status_code=status.HTTP_303_SEE_OTHER)
    label = followups.window_label(when)
    # The owner hears about a booking (one of the few things worth an alert);
    # the homeowner gets a confirmation; the calendar gets the event.
    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=lead.get("name") or "", phone=lead.get("phone") or "",
            email=lead.get("email") or "", service=lead.get("project_type") or "", address=lead.get("address") or "",
            message=f"WALKTHROUGH BOOKED: {label}" + (f" — {note[:200]}" if note else ""), source="booking",
        )
    except Exception as e:
        print(f"⚠️ booking owner notify failed: {e}", flush=True)
    to = (lead.get("email") or "").strip()
    if to and not to.lower().endswith("@lead.local"):
        try:
            cfg = documents_service.smtp_config_from_env()
            if documents_service.smtp_configured(cfg):
                body = (f"Hi {followups._first_name(lead.get('name'))},\n\nYou're booked: {label}.\n"
                        f"Address on file: {lead.get('address') or 'we will confirm'}.\n\n"
                        f"Shaun will text when he's on the way. Need to change it? Reply to this email or call/text "
                        f"{os.getenv('COMPANY_PHONE', '+1 (757) 908-7121')}.\n\n{brand.name()}")
                await documents_service.send_email(cfg, to, f"Walkthrough booked — {label}", body.replace("\n", "<br>"))
        except Exception as e:
            print(f"⚠️ booking confirmation failed: {e}", flush=True)
    try:
        await asyncio.to_thread(zoho_sync.on_booking, db, lead_id, label, note)
    except Exception as e:
        print(f"⚠️ booking zoho sync failed: {e}", flush=True)
    placed = False
    try:
        import zoho_calendar
        if zoho_calendar.available():
            end = when + timedelta(hours=3)
            placed = await asyncio.to_thread(
                zoho_calendar.create_event,
                f"Walkthrough — {lead.get('name') or 'homeowner'} ({lead.get('project_type') or 'project'})",
                when, end, location=lead.get("address") or "",
                description=f"Lead #{lead_id}. {lead.get('phone') or ''} {lead.get('email') or ''}\n{note or ''}".strip())
    except Exception as e:
        print(f"⚠️ booking zoho calendar failed: {e}", flush=True)
    try:
        import google_calendar
        owner = (os.getenv("GMAIL_OWNER") or os.getenv("SMTP_FROM") or "").strip()
        if owner and not placed:
            end = when + timedelta(hours=3)
            google_calendar.create_event("construction", owner, os.getenv("BOOKING_CALENDAR_ID", "primary"), {
                "summary": f"Walkthrough — {lead.get('name') or 'homeowner'} ({lead.get('project_type') or 'project'})",
                "location": lead.get("address") or "",
                "description": f"Lead #{lead_id}. {lead.get('phone') or ''} {lead.get('email') or ''}\n{note or ''}".strip(),
                "start": {"dateTime": when.isoformat()}, "end": {"dateTime": end.isoformat()},
            })
    except Exception as e:
        print(f"⚠️ booking calendar event failed: {e}", flush=True)
    return RedirectResponse(url=f"/book/{token}?saved=1", status_code=status.HTTP_303_SEE_OTHER)


# --- call lists ------------------------------------------------------------------
# The bot fills the pipeline; a person still has to ring people. These two routes
# are the only place a crew member touches lead data, and the token is scoped to
# one crew member and one day (call_list.call_token), so a forwarded link cannot
# read tomorrow's list or anyone else's.

@app.get("/calllist/{token}", response_class=HTMLResponse)
async def call_list_page(token: str, request: Request, db=Depends(get_db)):
    parsed = call_list.verify_call_token(token)
    if not parsed:
        return templates.TemplateResponse(request=request, name="calllist.html",
                                          context={"invalid": True}, status_code=404)
    crew_id, day = parsed
    with db.cursor() as cur:
        cur.execute("SELECT id, name, phone FROM crew WHERE id = %s;", (crew_id,))
        crew = cur.fetchone()
    if not crew:
        return templates.TemplateResponse(request=request, name="calllist.html",
                                          context={"invalid": True}, status_code=404)
    return templates.TemplateResponse(request=request, name="calllist.html", context={
        "token": token,
        "crew": crew,
        "day_label": day.strftime("%A %-d %B"),
        "calls": call_list.list_for(db, crew_id, "construction", day),
        "progress": call_list.progress(db, crew_id, "construction", day),
        "saved": request.query_params.get("saved", ""),
    })


@app.post("/calllist/{token}/{assignment_id}")
async def call_list_mark(token: str, assignment_id: int, request: Request,
                         state: str = Form(""), note: str = Form(""), db=Depends(get_db)):
    parsed = call_list.verify_call_token(token)
    if not parsed:
        raise HTTPException(status_code=404, detail="That call list link is not valid.")
    crew_id, day = parsed
    # The token names the crew member, so the assignment must belong to them:
    # otherwise a guessed id would let one person write another's call log.
    with db.cursor() as cur:
        cur.execute("SELECT crew_id FROM call_assignments WHERE id = %s;", (assignment_id,))
        row = cur.fetchone()
    if not row or int(row["crew_id"] if isinstance(row, dict) else row[0]) != int(crew_id):
        raise HTTPException(status_code=404, detail="That call is not on this list.")
    call_list.mark(db, assignment_id, state, note)
    return RedirectResponse(url=f"/calllist/{token}?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/calllist/assign")
async def call_list_assign(request: Request, crew_id: str = Form(""), limit: str = Form("25"),
                           day: str = Form(""), text: str = Form("1"), db=Depends(get_db)):
    """Build a crew member's list for a day and (by default) text them the link.

    One row per person, and the text is the whole point: a list nobody is told
    about is a list nobody works.
    """
    require_admin(request)
    try:
        cid = int(crew_id or 0)
        cap = max(1, min(int(limit or 25), 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="crew_id and limit must be numbers.")
    target = None
    if day.strip():
        try:
            target = date.fromisoformat(day.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="day must be YYYY-MM-DD.")
    with db.cursor() as cur:
        cur.execute("SELECT id, name, phone FROM crew WHERE id = %s AND is_active;", (cid,))
        crew = cur.fetchone()
    if not crew:
        raise HTTPException(status_code=404, detail="No active crew member with that id.")
    res = call_list.assign(db, cid, "construction", cap, target)
    sent = False
    phone = (crew.get("phone") or "").strip()
    if text.strip() not in ("0", "false", "off") and phone:
        try:
            body = call_list.sms_text(crew.get("name") or "", cid, res["total"], target)
            sent = bool(signalwire.send_sms(phone, body))
        except Exception as e:
            print(f"⚠️ call list text failed: {e}", flush=True)
    return {"ok": True, **res, "crew": crew.get("name"), "texted": sent,
            "url": call_list.call_url(cid, target)}


def _form_error_redirect(path: str, error: str, **fields) -> RedirectResponse:
    """Bounce back to a public form with the error AND what was typed.

    /finance and /get-started are plain POST forms that redirect on a
    validation error. Their templates refill every field from the query
    string, but the redirects used to carry only ?error=, so a homeowner who
    missed one required field got "please fix the issue" on a blank form and
    had to type everything again. Carry the submission along (capped, since
    it rides in a URL).
    """
    params = {"error": error}
    for key, value in fields.items():
        value = (value or "").strip()
        if value:
            params[key] = value[:500]
    return RedirectResponse(url=f"{path}?{urllib.parse.urlencode(params)}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.get("/finance", response_class=HTMLResponse)
async def finance_page(request: Request):
    """Project-financing application page.

    This template has existed since before the public site went live and the nav
    has linked it from every page, but no route was ever registered for it -- so
    "Financing" 404'd sitewide and the intake form underneath it was unreachable.
    Unlike /instant-quote, financing is usually the reason someone cannot start a
    project at all, which makes this one of the few pages that turns a stalled
    "maybe later" into a live lead.
    """
    return templates.TemplateResponse(request=request, name="finance.html", context={})


@app.post("/finance")
async def finance_submit(
    full_name: str = Form(""),
    phone: str = Form(""),
    project_type: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    amount_needed: str = Form(""),
    loan_purpose: str = Form("home"),
    when_start: str = Form(""),
    message: str = Form(""),
    company_website: str = Form(""),
    db=Depends(get_db),
):
    """Record a financing application as a lead, then confirm in-page.

    Mirrors /submit-lead exactly -- same intake, same owner notification, same
    auto-reply, same follow-up scheduling -- so an application is worked by the
    pipeline like any other inbound lead. The funding_* columns are what the rest
    of the app already keys off to flag and sort money-motivated leads.

    The three required fields are declared optional on purpose so a blank
    submission lands on this page's own ?error= banner. Marking them
    Form(...) would hand a homeowner who just pressed Submit a raw JSON
    validation error instead.
    """
    if _is_form_bot(company_website, "finance"):
        return RedirectResponse(url="/finance?sent=1", status_code=status.HTTP_303_SEE_OTHER)
    name = (full_name or "").strip()
    typed = dict(full_name=full_name, phone=phone, project_type=project_type, email=email,
                 address=address, amount_needed=amount_needed, message=message)
    if not name or not (phone or "").strip() or not (project_type or "").strip():
        return _form_error_redirect("/finance", "Name, phone and project type are required", **typed)

    amount_cents = None
    raw_amount = (amount_needed or "").strip()
    if raw_amount:
        try:
            amount_cents = int(float(raw_amount) * 100)
        except ValueError:
            # A budget is a nice-to-have; never lose the application over one.
            amount_cents = None

    try:
        res = lead_intake.intake_lead(
            db, company="construction", source="financing", name=name, phone=phone,
            email=(email or "").strip(), address=address,
            description=message, project_type=project_type,
            extra={
                "budget": raw_amount,
                "timeline": when_start,
                "funding_needed": True,
                "funding_amount_cents": amount_cents,
                "funding_use": message or project_type,
            },
        )
    except Exception as exc:
        logger.warning("[finance] intake failed: %s", exc)
        return _form_error_redirect("/finance", "We could not save that just now", **typed)

    lead_id = res["lead_id"]

    # Business/investment financing is the one purpose we may be paid a
    # referral fee on. Record the referral so it has a lifecycle the owner
    # can advance from the admin page; a purely residential application stays
    # a lead only. funding_referrals normalizes the purpose and refuses to
    # default toward the paid one, so an ambiguous answer lands on 'home'.
    # Isolated: a failure here must not cost us the lead we just captured.
    try:
        purpose = funding_referrals.normalize_purpose(loan_purpose)
        if purpose == funding_referrals.BUSINESS:
            funding_referrals.create(
                db, lead_id=lead_id, purpose=purpose,
                amount_cents=amount_cents, consent_at=datetime.now(timezone.utc),
                consent_note="finance form — business/investment self-identified",
                note=(message or project_type or "")[:500],
            )
    except Exception as e:
        print(f"[finance] referral record failed: {e}", flush=True)

    if res.get("created"):
        # Same side effects as /submit-lead. Each is isolated: a failed
        # notification must not cost us the lead we just captured.
        try:
            auto_reply.notify_owner_email(
                "construction", lead_id=lead_id, name=name, phone=phone,
                email=(email or "").strip(), service=project_type, address=address,
                budget=raw_amount, timeline=when_start, message=message,
                source="financing",
            )
        except Exception as e:
            print(f"[finance] owner notify failed: {e}", flush=True)
        try:
            auto_reply.auto_reply_to_lead(
                db, "construction", name=name, phone=phone, email=(email or "").strip(),
                service=project_type, address=address, budget=raw_amount,
                timeline=when_start, message=message, source="financing",
                lead_id=lead_id,
            )
        except Exception as e:
            print(f"[finance] auto-reply failed: {e}", flush=True)
        try:
            _follow_up_new_lead(lead_id)
        except Exception as e:
            print(f"[finance] follow-up failed: {e}", flush=True)
        await asyncio.to_thread(zoho_sync.after_intake, db, lead_id)
    else:
        # A repeat application is still worth flagging to the owner -- it is a
        # second signal of intent, not a duplicate to discard silently.
        try:
            auto_reply.notify_owner_email(
                "construction", lead_id=lead_id, name=name, phone=phone,
                email=(email or "").strip(), service=project_type, address=address,
                budget=raw_amount, timeline=when_start,
                message=f"[repeat financing application] {message}".strip(),
                source="financing",
            )
        except Exception as e:
            print(f"[finance] repeat notify failed: {e}", flush=True)

    return RedirectResponse(url="/finance?sent=1", status_code=status.HTTP_303_SEE_OTHER)


# --- Funding referrals: the revenue side of /finance ------------------------
# Business/SBA loan referrals and the commission a lender pays us when one
# funds. Owner-only. The lifecycle and the business-purpose guard live in
# funding_referrals.py; these routes only drive it and read it back.

@app.get("/finance/referrals", response_class=HTMLResponse)
async def funding_referrals_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    funding_referrals.ensure_schema(db)
    rows = funding_referrals.list_referrals(db)
    totals = {
        "open": sum(1 for r in rows if r["status"] in (funding_referrals.NEW, funding_referrals.SUBMITTED)),
        "funded": sum(1 for r in rows if r["status"] in (funding_referrals.FUNDED, funding_referrals.COMMISSION_PAID)),
        "commission_cents": sum(int(r.get("commission_cents") or 0) for r in rows),
    }
    return templates.TemplateResponse(
        request=request, name="funding_referrals.html",
        context={"rows": rows, "totals": totals,
                 "statuses": funding_referrals.STATUSES,
                 "purpose_business": funding_referrals.BUSINESS,
                 "today": date.today()})


@app.post("/finance/referrals/{referral_id}/status")
async def funding_referral_status(referral_id: int, request: Request,
                                  status_: str = Form("", alias="status"),
                                  partner: str = Form(""),
                                  commission_pct: str = Form(""),
                                  note: str = Form(""),
                                  db=Depends(get_db)):
    """Advance one referral. On `commission_paid`, book the fee to the ledger.

    The guard is in funding_referrals.set_status -- a home-purpose referral
    cannot reach a business-only status, so this route cannot be used to book
    an unlicensed referral fee even by mistake. The ledger post is idempotent
    on the referral id, so pressing the button twice does not double-count.
    """
    require_admin(request)
    funding_referrals.ensure_schema(db)
    try:
        row = funding_referrals.get(db, referral_id)
        if partner.strip() and row:
            with db.cursor() as cur:
                cur.execute("UPDATE funding_referrals SET partner = %s, updated_at = NOW() WHERE id = %s",
                            (partner.strip()[:120], referral_id))
            db.commit()
        pct = None
        if commission_pct.strip():
            try:
                pct = float(commission_pct.strip().replace("%", "").replace(",", ""))
            except ValueError:
                pct = None
        updated = funding_referrals.set_status(
            db, referral_id, status_, note=note, commission_pct=pct)
        if updated["status"] == funding_referrals.COMMISSION_PAID:
            try:
                ledger_bridge.post_referral_commission(db, updated)
            except ledger.LedgerError as e:
                print(f"[funding-ref] commission not booked: {e}", flush=True)
    except funding_referrals.ReferralGuardError as e:
        back = request.headers.get("referer") or "/finance/referrals"
        sep = "&" if "?" in back else "?"
        return RedirectResponse(url=f"{back}{sep}error={str(e)[:200]}",
                                status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/finance/referrals", status_code=status.HTTP_303_SEE_OTHER)


# --- Auth -------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def read_login(request: Request):
    actor = current_actor(request)
    if actor:
        return RedirectResponse(url=_login_redirect(actor.get("role")), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="login.html", context={
        "error": request.query_params.get("error"),
    })


@app.post("/api/auth/login")
async def api_login(identifier: str = Form(...), secret: str = Form(...), request: Request = None, db=Depends(get_db)):
    actor = _resolve_credentials(db, identifier, secret)
    if not actor:
        return RedirectResponse(url="/login?error=Invalid+email+or+password", status_code=status.HTTP_303_SEE_OTHER)
    email = (actor.get("email") or "").strip()
    if _otp_enabled(db) and not email:
        return RedirectResponse(url="/login?error=No+email+on+file", status_code=status.HTTP_303_SEE_OTHER)

    if not _otp_enabled(db):
        return _finish_login(actor, request)

    code = auth_service.generate_otp()
    _store_otp(db, actor["role"], actor.get("id"), email, code)
    ok, err = await _deliver_otp(email, code, actor)
    if not ok:
        return RedirectResponse(url=f"/login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending(actor["role"], actor.get("id"), email, actor.get("name")),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp


@app.get("/login/otp", response_class=HTMLResponse)
async def read_otp(request: Request):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="otp.html", context={
        "error": request.query_params.get("error"),
        "resent": request.query_params.get("resent"),
        "email": pending.get("email"),
        "masked_email": _mask_email(pending.get("email") or ""),
        "name": pending.get("name"),
    })


@app.post("/api/auth/verify-otp")
async def api_verify_otp(request: Request, code: str = Form(...), db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM login_otps WHERE role = %s AND actor_id IS NOT DISTINCT FROM %s AND consumed = FALSE ORDER BY created_at DESC LIMIT 1;",
            (pending["role"], pending.get("id")),
        )
        row = cur.fetchone()
    if not row or (row["expires_at"] and row["expires_at"] < datetime.now(APP_TZ)):
        return RedirectResponse(url="/login/otp?error=Code+expired+%E2%80%94+resend+below", status_code=status.HTTP_303_SEE_OTHER)
    if row["attempts"] >= auth_service.MAX_OTP_ATTEMPTS:
        return RedirectResponse(url="/login?error=Too+many+attempts+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    if not auth_service.verify_otp((code or "").strip(), row["code_hash"]):
        with db.cursor() as cur:
            cur.execute("UPDATE login_otps SET attempts = attempts + 1 WHERE id = %s;", (row["id"],))
            db.commit()
        return RedirectResponse(url="/login/otp?error=Incorrect+code", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute("UPDATE login_otps SET consumed = TRUE WHERE id = %s;", (row["id"],))
        db.commit()
    return _finish_login({
        "role": pending["role"], "id": pending.get("id"),
        "email": pending.get("email"), "name": pending.get("name"),
    }, request)


@app.post("/api/auth/resend-otp")
async def api_resend_otp(request: Request, db=Depends(get_db)):
    pending = auth_service.pending_from_token(request.cookies.get(auth_service.OTP_COOKIE))
    if not pending:
        return RedirectResponse(url="/login?error=Verification+expired+%E2%80%94+please+sign+in+again", status_code=status.HTTP_303_SEE_OTHER)
    code = auth_service.generate_otp()
    _store_otp(db, pending["role"], pending.get("id"), pending.get("email"), code)
    ok, err = await _deliver_otp(pending.get("email"), code, {"name": pending.get("name")})
    if not ok:
        return RedirectResponse(url=f"/login?error={urllib.parse.quote(err)}", status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/login/otp?resent=1", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=auth_service.OTP_COOKIE,
        value=auth_service.issue_pending(pending["role"], pending.get("id"), pending.get("email"), pending.get("name")),
        httponly=True, samesite="lax", secure=_secure_cookies(), max_age=auth_service.OTP_TTL_SECONDS,
    )
    return resp


@app.get("/api/auth/logout")
async def api_logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(auth_service.SESSION_COOKIE)
    response.delete_cookie("user_email")
    response.delete_cookie("user_name")
    return response


# --- GOOGLE OAUTH ---

@app.get("/oauth/zoho/start")
async def zoho_oauth_start(request: Request):
    """Connect Zoho with every scope the app uses (CRM, Books, Campaigns, Mail,
    Calendar). One click from Settings; the refresh token is stored in
    app_settings and preferred over the one in Railway."""
    require_admin(request)
    if not zoho_oauth.configured():
        raise HTTPException(status_code=400, detail="ZOHO_CLIENT_ID / ZOHO_CLIENT_SECRET are not set")
    return RedirectResponse(url=zoho_oauth.auth_url(zoho_oauth.new_state()), status_code=status.HTTP_303_SEE_OTHER)


@app.get("/oauth/zoho/callback")
async def zoho_oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    require_admin(request)
    if error:
        return RedirectResponse(url=f"/settings?error={urllib.parse.quote('Zoho: ' + error)}#zoho", status_code=status.HTTP_303_SEE_OTHER)
    if not code or not zoho_oauth.consume_state(state):
        return RedirectResponse(url="/settings?error=Zoho+sign-in+did+not+complete+(state)#zoho", status_code=status.HTTP_303_SEE_OTHER)
    try:
        tokens = await asyncio.to_thread(zoho_oauth.exchange_code, code)
    except Exception as e:
        return RedirectResponse(url=f"/settings?error={urllib.parse.quote('Zoho: ' + str(e)[:160])}#zoho", status_code=status.HTTP_303_SEE_OTHER)
    actor = current_actor(request) or {}
    zoho_oauth.save_connection(tokens["refresh_token"], tokens.get("scope", ""), str(actor.get("email") or ""))
    try:
        import zoho_client
        zoho_client.invalidate_token_cache()
    except Exception:
        pass
    return RedirectResponse(url="/settings?saved=zoho#zoho", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/settings/zoho/grant")
async def settings_zoho_grant(request: Request, grant_code: str = Form(""), db=Depends(get_db)):
    """Connect Zoho from a Self Client grant code pasted on Settings."""
    require_admin(request)
    code = (grant_code or "").strip()
    if not code:
        return RedirectResponse(url="/settings?error=Paste+the+grant+code#zoho", status_code=status.HTTP_303_SEE_OTHER)
    try:
        tokens = await asyncio.to_thread(zoho_oauth.exchange_grant_code, code)
    except Exception as e:
        return RedirectResponse(url=f"/settings?error={urllib.parse.quote(str(e)[:180])}#zoho", status_code=status.HTTP_303_SEE_OTHER)
    actor = current_actor(request) or {}
    zoho_oauth.save_connection(tokens["refresh_token"], tokens.get("scope", ""), str(actor.get("email") or ""))
    try:
        import zoho_client
        zoho_client.invalidate_token_cache()
    except Exception:
        pass
    return RedirectResponse(url="/settings?saved=zoho#zoho", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/settings/zoho")
async def settings_zoho(request: Request, list_key: str = Form(""), db=Depends(get_db)):
    """Pick the Campaigns mailing list inbound leads are added to."""
    require_admin(request)
    zoho_campaigns.set_list_key(db, list_key)
    return RedirectResponse(url="/settings?saved=zoho#zoho", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/oauth/autodesk/start")
async def autodesk_oauth_start(request: Request):
    """Send the owner to Autodesk to sign in.

    Admin only. A two-legged token cannot read ACC project data, so the owner
    has to grant access as themselves -- there is no way to wire this without
    one human clicking through it once.

    State and the PKCE verifier both go in httponly cookies rather than the
    query string. The verifier is a secret: anything that leaks it can redeem
    the authorization code, and the code arrives on a URL that ends up in
    browser history and server logs.
    """
    require_admin(request)
    if not autodesk_oauth.configured():
        return RedirectResponse(
            url="/bids?error=Autodesk+is+not+configured%3A+"
                + "+".join(autodesk_oauth.missing()),
            status_code=status.HTTP_303_SEE_OTHER)
    state, verifier, challenge = autodesk_oauth.new_state()
    resp = RedirectResponse(url=autodesk_oauth.build_auth_url(state, challenge),
                            status_code=status.HTTP_303_SEE_OTHER)
    for name, val in ((autodesk_oauth.STATE_COOKIE, state),
                      ("aps_pkce_verifier", verifier)):
        resp.set_cookie(name, val, httponly=True, samesite="lax",
                        secure=_secure_cookies(), max_age=autodesk_oauth.STATE_TTL)
    return resp


@app.get("/oauth/autodesk/callback")
async def autodesk_oauth_callback(request: Request, code: str = None, error: str = None):
    """Take the code back, store the token, land on /bids.

    Deliberately unauthenticated on the request itself: APS redirects here from
    the browser, and the session cookie is what proves the callback belongs to
    the sign-in we started. State is compared with compare_digest and a missing
    cookie fails rather than defaults -- an absent state is a failure, not an
    empty string to be matched against.
    """
    import hmac

    state = request.cookies.get(autodesk_oauth.STATE_COOKIE)
    verifier = request.cookies.get("aps_pkce_verifier")
    got_state = request.query_params.get("state")
    if not state or not got_state or not hmac.compare_digest(state, got_state):
        return RedirectResponse(url="/bids?error=Autodesk+state+mismatch%3A+"
                                       "start+the+connection+again",
                                status_code=status.HTTP_303_SEE_OTHER)
    if error:
        return RedirectResponse(url=f"/bids?error=Autodesk+cancelled%3A+{error}",
                                status_code=status.HTTP_303_SEE_OTHER)
    if not code:
        return RedirectResponse(url="/bids?error=No+authorization+code+from+Autodesk",
                                status_code=status.HTTP_303_SEE_OTHER)
    if not verifier:
        return RedirectResponse(url="/bids?error=Missing+PKCE+verifier%3A+"
                                       "cookies+may+be+blocked+on+this+callback",
                                status_code=status.HTTP_303_SEE_OTHER)
    try:
        tok = autodesk_oauth.exchange_code(code, verifier)
        autodesk_oauth.save_tokens(
            user_email=_owner_email(),
            access_token=tok["access_token"],
            refresh_token=tok.get("refresh_token"),
            expires_in=tok.get("expires_in", 3600),
            scope=tok.get("scope", autodesk_oauth.SCOPE_STRING),
        )
    except autodesk_oauth.AutodeskError as e:
        return RedirectResponse(url=f"/bids?error=Autodesk+exchange+failed%3A+{e}",
                                status_code=status.HTTP_303_SEE_OTHER)
    resp = RedirectResponse(url="/bids?aps=connected", status_code=status.HTTP_303_SEE_OTHER)
    for name in (autodesk_oauth.STATE_COOKIE, "aps_pkce_verifier"):
        resp.delete_cookie(name)
    return resp


@app.get("/api/autodesk/status")
async def autodesk_status(request: Request):
    """Is Autodesk wired, is it connected, and which projects can it see.

    The project list is here on purpose: APS_PROJECT_ID is otherwise something
    you go hunting for in the ACC UI, and this returns it with the name beside
    it. Admin only -- it reads a live token.
    """
    require_admin(request)
    out = {
        "configured": autodesk_oauth.configured(),
        "missing": autodesk_oauth.missing(),
        "connected": bool(autodesk_oauth.get_tokens(_owner_email())),
        "project_id": autodesk_oauth.config()["project_id"],
        "projects": [],
        "error": None,
    }
    if out["connected"] and not out["project_id"]:
        try:
            out["projects"] = [
                {"id": p.get("id"), "name": p.get("name"),
                 "status": p.get("status"), "accountId": p.get("accountId")}
                for p in autodesk_oauth.list_projects(_owner_email())
            ]
        except autodesk_oauth.AutodeskError as e:
            out["error"] = str(e)
    return JSONResponse(content=out)


@app.get("/auth/google")
async def google_oauth_start(request: Request, service: str = "construction"):
    """Initiate Google OAuth flow."""
    if service != "construction":
        return RedirectResponse(url="/login?error=Invalid+service", status_code=status.HTTP_303_SEE_OTHER)
    auth_url, state = google_oauth.start_google_oauth(service)
    resp = RedirectResponse(url=auth_url, status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie("google_oauth_state", state, httponly=True, samesite="lax", secure=_secure_cookies(), max_age=600)
    resp.set_cookie("google_oauth_service", service, httponly=True, samesite="lax", secure=_secure_cookies(), max_age=600)
    return resp


@app.get("/auth/google/callback")
async def google_oauth_callback(request: Request, code: str = None, error: str = None, db=Depends(get_db)):
    """Handle Google OAuth callback."""
    state = request.cookies.get("google_oauth_state")
    service = request.cookies.get("google_oauth_service", "construction")
    if error:
        return RedirectResponse(url=f"/login?error=Google+OAuth+cancelled%3A+{error}", status_code=status.HTTP_303_SEE_OTHER)
    if not code:
        return RedirectResponse(url="/login?error=No+authorization+code+from+Google", status_code=status.HTTP_303_SEE_OTHER)
    try:
        user = google_oauth.handle_google_callback(service, code, state)
        user_email = user["user_email"]
        user_name = user["user_name"]
        # Resolve or create local account
        actor = None
        with db.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE email ILIKE %s;", (user_email,))
            actor = cur.fetchone()
        if not actor:
            # Least privilege. This used to be `else "worker"`, which meant any
            # Google account that completed a sign-in was granted the crew app:
            # time sheets, payroll, internal jobs. A customer clicking "sign in
            # with Google" on the sandbox would have landed inside the business.
            # An unrecognised account is now a customer, and crew have to be
            # invited by an admin.
            role = "admin" if is_admin_email(user_email) else "customer"
            with db.cursor() as cur:
                cur.execute("""
                    INSERT INTO users (email, name, role, created_at)
                    VALUES (%s, %s, %s, NOW()) RETURNING *;
                """, (user_email, user_name, role))
                actor = cur.fetchone()
                db.commit()
        if not actor:
            return RedirectResponse(url="/login?error=Failed+to+create+account", status_code=status.HTTP_303_SEE_OTHER)
        role = actor.get("role", "worker")
        actor_dict = {"role": role, "id": actor["id"], "email": user_email, "name": user_name}
        return _finish_login(actor_dict, request)
    except Exception as e:
        print(f"[GOOGLE OAUTH] Callback error: {e}")
        return RedirectResponse(url=f"/login?error=Google+sign-in+failed", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/google/revoke")
async def google_revoke_access(request: Request, db=Depends(get_db)):
    """Revoke Google access for current user."""
    actor = auth_service.actor_from_token(request.cookies.get(auth_service.SESSION_COOKIE))
    if not actor:
        return JSONResponse({"status": "error", "message": "Not logged in"}, status_code=401)
    service = "construction"
    google_oauth.revoke_google_access(service, actor["email"])
    return JSONResponse({"status": "success", "message": "Google access revoked"})


# --- Admin: dashboard + leads ----------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    # One home screen. The old dashboard was a second, partial view of the
    # same numbers; Today (/pipeline) is the owner's landing page.
    return RedirectResponse(url="/pipeline", status_code=status.HTTP_303_SEE_OTHER)

    vis = source_visibility_clause()
    vis_params = tuple(source_visibility_params())
    with db.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM leads WHERE company = 'construction'{vis};", vis_params)
        total_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status = 'new' AND company = 'construction'"
                    + vis + ";", vis_params)
        new_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status IN ('quoted','deposit','in_progress')"
                    f" AND company = 'construction'{vis};", vis_params)
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments WHERE company = 'construction';")
        pay = cur.fetchone()
        cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at"
                    f" FROM leads WHERE company = 'construction'{vis} ORDER BY created_at DESC LIMIT 8;",
                    vis_params)
        recent = cur.fetchall()

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "user": {"email": user_email, "name": current_actor(request).get("name") if current_actor(request) else ""},
        "stats": {
            "total_leads": total_leads,
            "new_leads": new_leads,
            "active": active,
            "deposits_count": pay["c"],
            "deposit_total": pay["amt"],
        },
        "recent": recent,
        "statuses": LEAD_STATUSES,
        "status_labels": STATUS_LABELS,
    })


@app.get("/leads", response_class=HTMLResponse)
async def leads_page(request: Request,
                     # Every link into this page (Pipeline stage cards, the permits
                     # lane) passes ?status=won / ?status=quoted. The parameter was
                     # named status_filter with no alias, so FastAPI dropped the
                     # value and "Won (1)" opened the unfiltered queue. The name is
                     # kept (fastapi.status is in scope) and the alias restores the
                     # links.
                     status_filter: str = Query("", alias="status"),
                     q: str = "", lane: str = "contactable",
                     action: str = "", closed: str = "", db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Contactable lane = the shared work queue. Leads are grouped by the one
    # thing to do with them next (reply, follow up, first contact, get contact
    # info), most urgent first, instead of a flat list the owner triages by hand.
    # The app's own source-visibility policy is layered onto the shared query, so
    # a hidden source stays hidden without the package knowing about it.
    vis = source_visibility_clause()
    vis_where = vis[len(" AND "):] if vis.startswith(" AND ") else vis.strip()
    vis_params = source_visibility_params()
    rows = lead_store.list_leads(
        db, "construction", q=q, limit=500,
        include_closed=(closed == "1"),
        stage=(status_filter or None),
        where_extra=vis_where, extra_params=tuple(vis_params),
    )
    all_buckets = lead_queue.group(rows)
    action_counts = lead_queue.action_counts(all_buckets)
    buckets = ([b for b in all_buckets if b["action"] == action]
               if action in lead_lifecycle.ACTION_ORDER else all_buckets)
    draft_count = lead_store.draft_count(
        db, "construction", where_extra=vis_where, extra_params=tuple(vis_params))

    with db.cursor() as cur:
        # Intel lane: permits, which have no phone or email by nature.
        p_where, p_params = [], []
        if status_filter in PERMIT_STATUSES:
            p_where.append("status = %s")
            p_params.append(status_filter)
        if q.strip():
            like = f"%{q.strip()}%"
            p_where.append("(address ILIKE %s OR work_type ILIKE %s OR permit_number ILIKE %s OR contractor_name ILIKE %s)")
            p_params += [like, like, like, like]
        p_clause = ("WHERE " + " AND ".join(p_where)) if p_where else ""
        cur.execute(f"SELECT * FROM job_leads {p_clause} ORDER BY found_at DESC LIMIT 500;", tuple(p_params))
        permits = cur.fetchall()
        # Every permit tally is recomputed here, from job_leads, on every page
        # load. Nothing is cached, so a status change from ANY source -- the
        # owner dragging a card, the Copilot, the voice agent, /job-leads/refresh,
        # or a manual SQL edit -- is reflected the next time this renders.
        pstats = permit_stats.permit_stats(cur)
        permit_counts = pstats["workable_by_status"]
        # The headline is distinct ACTIONABLE DOORS, not rows: one house often
        # has several permits, and 862 permits sat at 655 doors.
        permit_open = pstats["actionable_doors"]
        permit_doors_total = pstats["actionable_doors"]
        permit_permits_total = pstats["actionable_permits"]
        permit_use = pstats["by_use_class"]
        permit_no_address = pstats["no_address"]
        permit_commercial = pstats["commercial"]
        permit_unreviewed = pstats["unreviewed"]
        permit_untouched = pstats["untouched_doors"]
        permit_touched = pstats["touched_doors"]
        permit_by_city = pstats["by_city"]
        # Owner of record for every permit whose address we have already traced.
        # Read from skiptrace_cache, so it is free and it persists: without this
        # "Who owns this property?" only ever showed the answer until the page
        # was reloaded, because the lookup deliberately does not write to the
        # permit.
        permit_owners = skiptrace_service.owners_for_rows(cur, permits)
        # Contact details we already hold for these addresses. The assessor
        # record gives a name but no way to reach anyone, which made it look
        # like a paid vendor was the only route -- but some of these leads are
        # already enriched, and nothing on the card said so.
        permit_contacts = contacts_at_addresses(cur, permits)

    return templates.TemplateResponse(request=request, name="leads.html", context={
        "user": {"email": user_email},
        "buckets": buckets,
        "action_counts": action_counts,
        "action_labels": lead_lifecycle.ACTION_LABELS,
        "active_action": action,
        "closed": closed == "1",
        # The status control on a card is the canonical lifecycle stage, not the
        # old seven-value property status. Business state (a deposit, a project)
        # is tracked by its own control, not by overloading this column.
        "stages": list(lead_lifecycle.STAGES),
        "stage_labels": lead_lifecycle.STAGE_LABELS,
        "active_status": status_filter,
        "q": q,
        "draft_count": draft_count,
        "default_deposit": default_deposit_cents() / 100,
        "permits": permits,
        "permit_owners": permit_owners,
        "permit_contacts": permit_contacts,
        "permit_statuses": PERMIT_STATUSES,
        "permit_status_labels": PERMIT_STATUS_LABELS,
        "permit_status_colors": PERMIT_STATUS_COLORS,
        "permit_counts": permit_counts,
        "permit_use": permit_use,
        "permit_no_address": permit_no_address,
        "permit_open": permit_open,
        "permit_doors_total": permit_doors_total,
        "permit_permits_total": permit_permits_total,
        "permit_commercial": permit_commercial,
        "permit_unreviewed": permit_unreviewed,
        "permit_untouched": permit_untouched,
        "permit_touched": permit_touched,
        "permit_by_city": permit_by_city,
        "shovels_configured": permit_service.is_configured(),
        "permit_status": _permit_status(),
        "lane": lane if lane in ("contactable", "permits") else "contactable",
    })


@app.get("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_workspace_page(lead_id: int, request: Request, db=Depends(get_db)):
    """One workspace for a lead's sales activity and connected job."""
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    visibility = source_visibility_clause("l")
    visibility_params = tuple(source_visibility_params())
    with db.cursor() as cur:
        cur.execute(
            "SELECT l.* FROM leads l WHERE l.id = %s AND l.company = 'construction'"
            + visibility + ";",
            (lead_id,) + visibility_params,
        )
        lead = cur.fetchone()
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")

        cur.execute(
            """SELECT p.*,
                      COALESCE((SELECT string_agg(c.name, ', ' ORDER BY c.name)
                                FROM project_assignments pa
                                JOIN crew c ON c.id = pa.crew_id
                                WHERE pa.project_id = p.id), '') AS crew_names,
                      (SELECT COUNT(*) FROM job_photos ph WHERE ph.project_id = p.id)
                          AS photo_count
               FROM projects p
               WHERE p.lead_id = %s
               ORDER BY p.created_at DESC
               LIMIT 1;""",
            (lead_id,),
        )
        project = cur.fetchone()

        cur.execute(
            "SELECT id, direction, channel, sender, recipient, message_body, created_at "
            "FROM comms_logs WHERE lead_id = %s "
            "OR (%s <> '' AND (LOWER(recipient) = LOWER(%s) OR LOWER(sender) = LOWER(%s))) "
            "OR (%s <> '' AND (LOWER(recipient) = LOWER(%s) OR LOWER(sender) = LOWER(%s))) "
            "ORDER BY created_at DESC LIMIT 30;",
            (
                lead_id,
                (lead.get("email") or "").strip(),
                (lead.get("email") or "").strip(),
                (lead.get("email") or "").strip(),
                (lead.get("phone") or "").strip(),
                (lead.get("phone") or "").strip(),
                (lead.get("phone") or "").strip(),
            ),
        )
        activity = cur.fetchall()

        cur.execute(
            "SELECT id, amount_cents, currency, status, created_at "
            "FROM payments WHERE lead_id = %s ORDER BY created_at DESC LIMIT 20;",
            (lead_id,),
        )
        payments = cur.fetchall()

    return templates.TemplateResponse(
        request=request,
        name="lead_workspace.html",
        context={
            "user": {"email": user_email or ""},
            "lead": lead,
            "project": project,
            "activity": activity,
            "payments": payments,
            "statuses": LEAD_STATUSES,
            "status_labels": STATUS_LABELS,
            "progress_statuses": [stage for stage in LEAD_STATUSES if stage != "lost"],
            "default_deposit": default_deposit_cents() / 100,
        },
    )


@app.get("/photo-quote", response_class=HTMLResponse)
async def photo_quote_page(request: Request):
    """Photo -> scope -> ballpark. Behind login.

    /api/vision/quote has existed since it was written but nothing in the
    templates referenced it, so it was reachable only by curl. The endpoint is
    left public on purpose -- its original design was a public upload, and
    gating it would remove a capability rather than add one.
    """
    is_authed, _ = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="photo_quote.html", context={})


@app.get("/skiptrace", response_class=HTMLResponse)
async def skiptrace_page(request: Request, permit_id: int = 0, db=Depends(get_db)):
    """Standalone owner-of-record lookup.

    The per-permit button on each card does the same thing, but it sits at the
    bottom of a collapsed card and was hard to find. This is one form, one URL.
    """
    is_authed, _ = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # Addresses the permits lane already knows, so the form is a picker rather
    # than four text boxes. Populated after load by /api/skiptrace/addresses.
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) AS c FROM job_leads "
            "WHERE NOT is_demo AND COALESCE(address,'') <> '';"
        )
        address_count = (cur.fetchone() or {}).get("c", 0)
        selected_permit_id = 0
        if permit_id > 0:
            cur.execute(
                "SELECT id FROM job_leads WHERE id = %s AND NOT is_demo "
                "AND COALESCE(address,'') <> '';",
                (permit_id,),
            )
            selected = cur.fetchone()
            selected_permit_id = int(selected["id"]) if selected else 0
    return templates.TemplateResponse(
        request=request, name="skiptrace.html",
        context={
            "address_count": address_count,
            "selected_permit_id": selected_permit_id,
        },
    )


@app.get("/inventory", response_class=HTMLResponse)
async def inventory_page(request: Request, company: str = "construction", db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    co = "construction"
    with db.cursor() as cur:
        cur.execute("SELECT * FROM supplies_inventory WHERE company = %s ORDER BY category, item_name;", (co,))
        rows = cur.fetchall()
    items = []
    for r in rows:
        qty = float(r.get("qty_on_hand") or 0)
        reorder = float(r.get("reorder_level") or 0)
        items.append({
            "id": r["id"], "item_name": r["item_name"], "category": r.get("category"),
            "qty_on_hand": qty, "unit": r.get("unit"), "reorder_level": reorder,
            "low": qty <= reorder,
        })
    return templates.TemplateResponse(request=request, name="inventory.html", context={
        "active_company": co, "items": items, "user": {"email": user_email},
    })


# --- Materials catalog (owner price book) ---------------------------------
# The supplier feeds are the destination, not the starting point: ABC Supply
# needs a credit account plus an approved enrollment, Lowe's a partner
# agreement, and Richards/Kempsville/Builders FirstSource have no feed at all.
# This page is how the price book gets real numbers before any of that lands.
# Prices are typed in dollars because that is what is printed on the receipt.


# --- Takeoffs: itemized material cost, owner's view only ---------------------
# Priced from every supplier including account-specific ones, which is the
# whole point. Everything here sits behind require_admin; the customer-facing
# half is material_picker and reads only public_ok suppliers.

@app.get("/api/takeoffs")
async def takeoffs_list(request: Request, bid_id: int = 0, db=Depends(get_db)):
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    return JSONResponse(content={"sheets": takeoff.list_sheets(db, bid_id or None, _BID_COMPANY)})


@app.post("/api/takeoffs")
async def takeoff_create(request: Request, name: str = Form(...), bid_id: int = Form(0),
                         markup_pct: float = Form(0.0), notes: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    if not name.strip():
        raise HTTPException(status_code=400, detail="name is required")
    sid = takeoff.create_sheet(db, name, bid_id or None, markup_pct, notes, _BID_COMPANY)
    return JSONResponse(content={"id": sid})


@app.get("/api/takeoffs/{sheet_id}")
async def takeoff_get(sheet_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    sheet = takeoff.get_sheet(db, sheet_id, _BID_COMPANY)
    if not sheet:
        raise HTTPException(status_code=404, detail="sheet not found")
    return JSONResponse(content={"sheet": {k: str(v) if hasattr(v, "isoformat") else v
                                           for k, v in sheet.items() if k != "lines"},
                                 "lines": [{k: str(v) if hasattr(v, "isoformat") else v
                                            for k, v in l.items()} for l in sheet["lines"]],
                                 "totals": takeoff.totals(sheet)})


@app.post("/api/takeoffs/{sheet_id}/lines")
async def takeoff_add_line(sheet_id: int, request: Request, description: str = Form(...),
                           qty: float = Form(1.0), item_number: str = Form(""),
                           uom: str = Form("each"), supplier: str = Form(""),
                           group_name: str = Form("other"), note: str = Form(""),
                           lookup: str = Form("0"), db=Depends(get_db)):
    """Add a line. lookup=1 fills the unit cost from the private price book."""
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    if not takeoff.get_sheet(db, sheet_id, _BID_COMPANY):
        raise HTTPException(status_code=404, detail="sheet not found")
    cents, sup = 0, supplier
    if lookup.lower() in ("1", "true", "yes") and item_number.strip():
        hit = takeoff.cost_lookup(db, [item_number.strip()], _BID_COMPANY).get(item_number.strip())
        if hit:
            cents, sup = hit["unit_cost_cents"], hit["supplier"]
    lid = takeoff.add_line(db, sheet_id, description, qty, item_number, uom,
                           cents, sup, "book" if cents else "unknown", group_name, note)
    return JSONResponse(content={"id": lid, "unit_cost_cents": cents, "supplier": sup})


@app.delete("/api/takeoffs/{sheet_id}/lines/{line_id}")
async def takeoff_delete_line(sheet_id: int, line_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    return JSONResponse(content={"ok": takeoff.delete_line(db, line_id, sheet_id)})


@app.get("/api/takeoffs/{sheet_id}/export.csv")
async def takeoff_export(sheet_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    takeoff.ensure_schema(conn=db)
    sheet = takeoff.get_sheet(db, sheet_id, _BID_COMPANY)
    if not sheet:
        raise HTTPException(status_code=404, detail="sheet not found")
    csv_text = takeoff.accounting_csv(sheet)
    return Response(content=csv_text, media_type="text/csv", headers={
        "Content-Disposition": f'attachment; filename="takeoff-{sheet_id}.csv"'})


# --- Books: double-entry reporting and the queue that keeps it honest -------
# Everything here is read-only except the repair actions. The numbers come
# from ledger.py, which enforces that debits equal credits.

@app.get("/books", response_class=HTMLResponse)
async def books_page(request: Request, as_of: str = "", db=Depends(get_db)):
    """Trial balance, balance sheet, income statement and per-job P&L."""
    require_admin(request)
    ledger_sync.ensure_schema(db)
    tb = ledger.trial_balance(db, as_of=as_of or None, company=_BID_COMPANY)
    bs = ledger.balance_sheet(db, as_of=as_of or None, company=_BID_COMPANY)
    ist = ledger.income_statement(db, company=_BID_COMPANY)
    return templates.TemplateResponse(
        request=request, name="books.html",
        context={"tb": tb, "bs": bs, "is": ist,
                 "jobs": ledger.job_pnl(db, company=_BID_COMPANY),
                 "entries": ledger.entries(db, 25, company=_BID_COMPANY),
                 "pending": ledger_sync.pending(db, company=_BID_COMPANY),
                 "legacy_unposted": [],
                 "as_of": as_of, "today": date.today()})


@app.post("/books/retry")
async def books_retry(request: Request, db=Depends(get_db)):
    """Re-attempt every queued posting. Idempotent, so safe to press twice."""
    require_admin(request)
    ledger_sync.ensure_schema(db)
    result = ledger_sync.retry_all(db, company=_BID_COMPANY)
    back = request.headers.get("referer") or "/books"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/books/expense")
async def books_expense(request: Request, amount: str = Form(...), account: str = Form("6010"),
                        description: str = Form(...), when: str = Form(...),
                        db=Depends(get_db)):
    """Post one evidenced historical/operating expense with its actual date."""
    require_admin(request)
    ledger_sync.ensure_schema(db)
    try:
        result = ledger_bridge.record_operating_expense(
            db, account, amount, description.strip(), when=when, company=_BID_COMPANY)
        if not result.get("posted") and result.get("reason") != "already posted":
            raise HTTPException(status_code=400, detail=result.get("reason", "Expense was not posted"))
    except (ValueError, ledger.LedgerError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/books/open")
async def books_open(request: Request, cash: str = Form(...), when: str = Form(""),
                     db=Depends(get_db)):
    """Post opening balances so the books start from a known position."""
    require_admin(request)
    ledger_sync.ensure_schema(db)
    try:
        ledger.open_books(db, float(cash.replace("$", "").replace(",", "")),
                          when=when or None, company=_BID_COMPANY)
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter the opening cash amount.")
    except ledger.LedgerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/books/repair")
async def books_repair_bid(bid_id: int, request: Request, db=Depends(get_db)):
    """Re-post everything on one job. For when a posting failed and was queued."""
    require_admin(request)
    ledger_sync.ensure_schema(db)
    result = ledger_sync.sync_all_for_bid(db, bid_id, company=_BID_COMPANY)
    if request.headers.get("accept", "").startswith("application/json"):
        return JSONResponse(content=result.as_dict())
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/bids/{bid_id}/invoices")
async def bid_invoices(bid_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    return JSONResponse(content={"invoices": bid_costing.list_invoices(db, bid_id, _BID_COMPANY),
                                 "summary": bid_costing.invoice_summary(db, bid_id, _BID_COMPANY)})


@app.post("/api/bids/{bid_id}/invoices")
async def bid_invoice_create(bid_id: int, request: Request, amount: str = Form(...),
                             description: str = Form(""), due_date: str = Form(""),
                             issue: int = Form(0), back: str = Form(""),
                             db=Depends(get_db)):
    """Raise an invoice. Posting to the ledger happens after the write and is
    queued rather than allowed to block the owner from billing a job."""
    require_admin(request)
    try:
        inv = bid_costing.create_invoice(db, bid_id, amount, _BID_COMPANY, description,
                                         due_date=due_date)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ledger_sync.ensure_schema(db)
    sync = ledger_sync.Sync()
    if issue:
        bid_costing.set_invoice_status(db, inv["id"], "sent", _BID_COMPANY)
        sync = ledger_sync.sync_invoice(db, inv["id"], _BID_COMPANY)
    if back.startswith("/bids/"):
        return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url=f"/bids?invoiced={inv['id']}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/invoices/{invoice_id}/status")
async def bid_invoice_status(bid_id: int, invoice_id: int, request: Request,
                             status: str = Form(...), collected: str = Form(""),
                             db=Depends(get_db)):
    require_admin(request)
    if not bid_costing.set_invoice_status(db, invoice_id, status, _BID_COMPANY,
                                          collected or None):
        raise HTTPException(status_code=404, detail="Invoice not found")
    ledger_sync.ensure_schema(db)
    ledger_sync.sync_invoice_status(db, invoice_id, _BID_COMPANY)
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/costs")
async def bid_cost_create(bid_id: int, request: Request, description: str = Form(...),
                          category: str = Form("material"), vendor: str = Form(""),
                          estimated: str = Form(""), actual: str = Form(""),
                          back: str = Form(""),
                          db=Depends(get_db)):
    require_admin(request)
    try:
        lid = bid_costing.add_cost_line(db, bid_id, category, description, vendor,
                                        estimated=estimated or None, actual=actual or None,
                                        company=_BID_COMPANY)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    ledger_sync.ensure_schema(db)
    if actual:
        ledger_sync.sync_cost_line(db, lid, _BID_COMPANY)
    # A cost entered from the bid page belongs to that bid, so put the owner
    # back where they were. Only ever follow a same-site path.
    if back.startswith("/bids/"):
        return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/costs/{line_id}/delete")
async def bid_cost_delete(bid_id: int, line_id: int, request: Request,
                          back: str = Form(""), db=Depends(get_db)):
    """Remove one cost line. Used to be unreachable -- the accessor existed but
    no route called it, so a wrong line could only be corrected in the database."""
    require_admin(request)
    if not bid_costing.delete_cost_line(db, line_id, bid_id, _BID_COMPANY):
        raise HTTPException(status_code=404, detail="Cost line not found")
    if back.startswith("/bids/"):
        return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/books", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/materials-picker/quote")
async def materials_picker_quote_form(request: Request):
    """The quote form, served inside the sandbox. Public."""
    return templates.TemplateResponse(
        request=request, name="quote_form.html",
        context={"project_types": INSTANT_PROJECT_TYPES})


@app.post("/api/materials-picker/quote")
async def materials_picker_quote(request: Request, items: str = Form("[]"),
                                address: str = Form(""), project_type: str = Form(""),
                                sqft: str = Form(""), db=Depends(get_db)):
    """Price the picked materials and estimate the labour around them.

    This is the sandbox's answer to the question a customer is actually
    asking: what does my project cost. It reuses the same engine as
    /instant-quote rather than inventing a second one, so a number from the
    sandbox and a number from the quote page are computed identically.
    """
    try:
        chosen = json.loads(items) if items else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="items must be a JSON array")
    if not isinstance(chosen, list):
        raise HTTPException(status_code=400, detail="items must be a JSON array")

    priced = material_picker.price_list(chosen)
    est = None
    try:
        manual = float(sqft or 0)
    except ValueError:
        manual = 0.0
    prop = property_service.lookup_address(address) if address.strip() else None
    if project_type.strip() and (prop or manual > 0):
        try:
            # Feed the picked items into the estimate as material-grade
            # choices. estimating_service already models a supplies premium
            # per grade, so a list of architectural shingles or premium
            # counters moves the labour-and-supplies number. Without this the
            # customer gets a supplies figure that ignores what they just
            # picked, which is exactly the number they are questioning.
            supplies = [ln.get("description") for ln in priced["lines"]
                        if ln.get("description")][:25]
            est = estimating_service.estimate(
                project_type.strip(), prop, manual or 0,
                materials=supplies or None)
        except Exception as e:
            logger.warning("sandbox estimate failed: %s", e)
            est = None

    estimate = _jsonable_estimate(est)
    if estimate:
        # quote_form.html reads low/high/total_low/total_high (cents) and
        # confidence. estimating_service names them low_cents/high_cents and
        # has no confidence field, so the card rendered nothing but
        # "Confidence: ballpark" -- the one number the visitor came for was
        # computed and then dropped on the floor.
        subtotal = int(priced.get("subtotal") or 0)
        low, high = estimate.get("low_cents"), estimate.get("high_cents")
        estimate.setdefault("low", low)
        estimate.setdefault("high", high)
        if low is not None and high is not None and subtotal:
            estimate.setdefault("total_low", int(low) + subtotal)
            estimate.setdefault("total_high", int(high) + subtotal)
        estimate.setdefault(
            "confidence",
            "property-based" if prop else ("square-footage" if manual > 0 else "ballpark"),
        )

    return JSONResponse(content={
        "materials_subtotal": priced["subtotal"],
        "lines": priced["lines"],
        "count": priced["count"],
        "estimate": estimate,
        "address": address.strip(),
        "project_type": project_type.strip(),
    })


def _jsonable_estimate(est):
    """The estimate is a nested model dict; make it survive JSON.

    Estimated totals are labelled as estimates everywhere they appear. A
    sandbox number that reads like a quote is how a homeowner ends up
    believing a range is a price.
    """
    if not est:
        return None
    if hasattr(est, "model_dump"):
        est = est.model_dump()
    elif not isinstance(est, dict):
        try:
            est = dict(est)
        except (TypeError, ValueError):
            return None

    def walk(v, depth=0):
        if depth > 4:
            return None
        if isinstance(v, dict):
            return {k: walk(x, depth + 1) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x, depth + 1) for x in v[:40]]
        if isinstance(v, (int, float, str, bool)) or v is None:
            return v
        return str(v)
    return walk(est)


@app.get("/materials-picker", response_class=HTMLResponse)
async def materials_picker_page(request: Request, search: str = "", category: str = ""):
    """Public material sandbox. No login.

    Reads only suppliers flagged public_ok, so an account price cannot reach
    this page. There is no parameter that changes that.
    """
    items = material_picker.browse(search=search, category=category)
    return templates.TemplateResponse(
        request=request, name="materials_picker.html",
        context={"items": items, "categories": material_picker.categories(),
                 "sel_cat": category or "all", "q": search,
                 "empty": not items})


@app.post("/api/materials-picker/price")
async def materials_picker_price(request: Request, items: str = Form("[]")):
    """Price a chosen list. Public, no login, no lead created."""
    try:
        chosen = json.loads(items) if items else []
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="items must be a JSON array")
    if not isinstance(chosen, list) or len(chosen) > 300:
        raise HTTPException(status_code=400, detail="send a list of at most 300 items")
    clean = []
    for it in chosen[:300]:
        if not isinstance(it, dict):
            continue
        clean.append({"item_number": str(it.get("item_number", ""))[:80],
                      "description": str(it.get("description", ""))[:300],
                      "uom": str(it.get("uom", "each"))[:40],
                      "qty": it.get("qty", 1)})
    return JSONResponse(content=material_picker.price_list(clean))


def _estimate_source(lines: list[dict]) -> str:
    """Classify what priced a list, from the suppliers that actually priced it.

    EstimationPro rows are published under a supplier row named for it, and
    those are a public benchmark, not a price anyone paid. Anything else came
    from a real supplier feed. Mixed lists are treated as the weaker of the
    two, because a document must not call a benchmark a real price.
    """
    names = [str(ln.get("supplier") or "").lower() for ln in lines if ln.get("extended")]
    if not names:
        return quoting.PRICE_SOURCE_ESTIMATE
    if all("estimationpro" in n for n in names):
        return quoting.PRICE_SOURCE_ESTIMATIONPRO
    if all("estimationpro" not in n for n in names):
        return quoting.PRICE_SOURCE_SUPPLIER
    return quoting.PRICE_SOURCE_ESTIMATIONPRO


@app.post("/api/materials-picker/submit")
async def materials_picker_submit(request: Request, name: str = Form(""),
                                  email: str = Form(""), phone: str = Form(""),
                                  notes: str = Form(""), items: str = Form("[]"),
                                  project_type: str = Form(""),
                                  width_ft: str = Form(""), length_ft: str = Form(""),
                                  sqft: str = Form(""),
                                  photo: str = Form(""),
                                  db=Depends(get_db)):
    """Hand the chosen list over. Creates a lead, and a quote when it can.

    The chosen list is also saved as a real quote with its pricing provenance
    recorded, so the customer's account has something to open and the number
    can be reproduced later. A quote that cannot be priced is not created and
    the reason is returned -- an unpriced quote is worse than none.
    """
    priced = material_picker.price_list(json.loads(items) if items else [])
    if not priced["lines"]:
        raise HTTPException(status_code=400, detail="Pick at least one material.")
    if not (name.strip() or email.strip() or phone.strip()):
        raise HTTPException(status_code=400, detail="Leave a name, email or phone number.")
    if email.strip() and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email.strip()):
        raise HTTPException(status_code=400, detail="That email does not look right.")
    made_lead = False
    lead_id = None
    # Email OR phone raises a lead. This used to require an email, so a
    # homeowner who left only a phone number got "Got it -- we'll be in touch"
    # and was never recorded anywhere. The quote below still needs an email
    # (it has to be findable from the account page), but the lead does not.
    if material_picker.submission_creates_lead() and (email.strip() or phone.strip()):
        lines = priced["lines"]
        summary = "; ".join(
            f"{(ln.get('description') or ln.get('item_number') or '?')}"
            f" x{ln.get('qty', 1)}"
            for ln in lines[:8]
        )
        if len(lines) > 8:
            summary += f"; +{len(lines) - 8} more"
        try:
            # price_list() already returns cents (customer_price() does), so the
            # old `* 100` stored every sandbox estimate at 100x.
            subtotal_cents = int(priced["subtotal"] or 0)
            res = lead_intake.intake_lead(
                db, company="construction", source="sandbox",
                name=name.strip(), phone=phone.strip(), email=email.strip(),
                description=f"Material list ({priced['count']} items): {summary}",
                project_type="materials",
                # Reuses existing columns rather than adding new ones.
                # store.insert_lead silently DROPS any field that is not already
                # a leads column, so a new key here would vanish with no error
                # and the selection would be lost. intake_lead has no `notes`
                # parameter -- it is a column, so it rides in `extra`.
                extra={
                    "notes": notes.strip()[:2000],
                    "estimate_low_cents": subtotal_cents,
                    "estimate_high_cents": subtotal_cents,
                    "estimate_json": json.dumps({
                        "kind": "sandbox_materials",
                        "count": priced["count"],
                        "subtotal_cents": subtotal_cents,
                        "lines": lines,
                    })[:8000],
                },
            )
            lead_id, made_lead = res["lead_id"], res["created"]
        except Exception as e:
            # A failed write must not lose their selection.
            print(f"⚠️ materials sandbox lead save failed: {e}", flush=True)
            made_lead = False
        if made_lead:
            try:
                zoho_sync.after_intake(db, lead_id)
            except Exception as e:
                print(f"⚠️ materials zoho sync failed: {e}", flush=True)
            # Every other public form tells the owner. A picked materials list
            # is the strongest buying signal the site collects, and it was the
            # one submission that notified nobody.
            try:
                auto_reply.notify_owner_email(
                    "construction", lead_id=lead_id, name=name.strip(),
                    phone=phone.strip(), email=email.strip(), service="materials",
                    message=f"Material list ({priced['count']} items, "
                            f"{_money(subtotal_cents)} est.): {summary}",
                    source="sandbox",
                )
            except Exception as e:
                print(f"⚠️ materials sandbox owner notify failed: {e}", flush=True)

    # A quote needs an email to be findable later, and needs a price to be a
    # quote at all. Lines that could not be priced are carried through as None
    # rather than zero, so the total is never quietly short.
    quote_id = quote_no = None
    quote_error = None
    priced_cents = sum(int(ln["extended"] or 0) for ln in priced["lines"])
    unpriced = sum(1 for ln in priced["lines"] if not ln.get("extended"))

    def _num(v):
        try:
            f = float(str(v or "").replace(",", "").strip())
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None

    w, l = _num(width_ft), _num(length_ft)
    # Width x length is the more specific claim, so it is not overridden by a
    # typed square footage. Implausible dimensions are refused rather than
    # written into a quote: a 900x900 ft property is a typo, not a job.
    bad_dims = bool(w and l and (w > 500 or l > 500))
    area = round(w * l, 2) if (w and l) else _num(sqft)

    if lead_id and priced_cents > 0 and bad_dims:
        quote_error = ("Those dimensions are over 500 ft, so no estimate was "
                       "created. Check the width and length and send again.")
    elif lead_id and priced_cents > 0:
        try:
            prov = quoting.price_provenance()
            body = quoting.build_quote(
                quoting.normalize_trade(project_type) or "materials",
                area, scope_text=notes.strip()[:2000],
                materials_tier="sandbox picks")
            # The customer's chosen list is the quote, not the band model, so
            # the model total is replaced by what they actually picked.
            body["job_price_cents"] = priced_cents
            body["deposit_cents"] = quoting.deposit_for(priced_cents)
            lines = []
            for ln in priced["lines"]:
                if not ln.get("extended"):
                    continue
                lines.append({"item_number": ln.get("item_number", ""),
                              "description": ln.get("description", ""),
                              "category": "material", "qty": ln.get("qty", 1),
                              "unit": ln.get("uom", "each"),
                              "unit_price_cents": int(ln.get("unit_price") or 0),
                              "total_cents": int(ln["extended"]),
                              "supplier": ln.get("supplier") or ""})
            saved = quoting.save_quote(
                db, lead_id, body, lines,
                customer_email=email.strip(), customer_name=name.strip(),
                width_ft=w, length_ft=l, photo_path=photo.strip()[:500],
                # Read the source off the lines rather than guessing from a
                # global flag: the line already knows which supplier priced it,
                # and that alone settles whether this is a real price or a
                # benchmark.
                price_source=_estimate_source(priced["lines"]),
                bls_series=prov.get("bls_series", ""),
                bls_period=prov.get("bls_period", ""),
                escalated=bool(prov.get("escalated")))
            quote_id, quote_no = saved["quote_id"], saved["quote_no"]
        except Exception as e:
            quote_error = str(e)[:200]
            print(f"⚠️ materials sandbox quote save failed: {e}", flush=True)
    elif lead_id and unpriced:
        quote_error = "None of the chosen items could be priced, so no quote was created."
    elif not priced_cents and priced["lines"]:
        quote_error = "The chosen items are not priced yet, so no quote was created."

    return JSONResponse(content={"ok": True, "count": priced["count"],
                                 "subtotal": priced["subtotal"],
                                 "lead": made_lead, "lead_id": lead_id,
                                 "quote_id": quote_id, "quote_no": quote_no,
                                 "quote_error": quote_error})


@app.get("/materials", response_class=HTMLResponse)
async def materials_page(request: Request, search: str = "", store: str = "",
                         category: str = "", notice: str = "", error: str = "",
                         db=Depends(get_db)):
    require_admin(request)
    materials_service.seed_materials_catalog(db)
    return templates.TemplateResponse(request=request, name="materials.html", context={
        "rows": materials_service.catalog_rows(
            db, search=search, store=store, category=category),
        "facets": materials_service.catalog_facets(db),
        "search": search, "store": store, "category": category,
        "notice": notice, "error": error,
        "abc_configured": abc_supply.configured(),
        "lowes_configured": lowes_api.configured(),
    })


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, notice: str = "", db=Depends(get_db)):
    """A customer's own quotes and documents.

    Scoped by the signed-in email. Admins see the same page for a customer by
    passing ?email=, which is how the owner looks up what someone was sent
    without opening their inbox.
    """
    actor = current_actor(request)
    email = (actor or {}).get("email") or ""
    is_admin = bool(actor and actor.get("role") == "admin")
    if is_admin and request.query_params.get("email"):
        email = request.query_params.get("email", "").strip().lower()
    if not email:
        return RedirectResponse(url="/login?next=/account",
                                status_code=status.HTTP_303_SEE_OTHER)

    try:
        quoting.ensure_schema(db)
        quotes = quoting.quotes_for_customer(db, email, limit=50)
    except Exception as e:
        print(f"⚠️ account page quotes failed: {e}", flush=True)
        quotes = []

    docs = []
    for q in quotes:
        for kind, label in (("estimate", "Estimate"),
                            ("bill_of_sale", "Bill of sale"),
                            ("receipt", "Receipt")):
            docs.append({"quote_id": q["id"], "quote_no": q["quote_no"],
                         "kind": kind, "label": label,
                         "url": f"/account/quotes/{q['id']}/{kind}"})

    return templates.TemplateResponse(request=request, name="account.html", context={
        "email": email,
        "quotes": quotes,
        "documents": docs,
        "notice": notice,
        "price_basis": quote_documents.PRICE_BASIS,
    })


@app.get("/account/quotes/{quote_id}/{kind}")
async def account_quote_document(request: Request, quote_id: int, kind: str,
                                  fmt: str = "pdf", db=Depends(get_db)):
    """Download one document for a quote the caller is entitled to."""
    actor = current_actor(request)
    is_admin = bool(actor and actor.get("role") == "admin")
    email = (actor or {}).get("email") or ""
    if not is_admin and not email:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    quoting.ensure_schema(db)
    quote = quoting.quote_with_lines(db, quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="No such quote")
    if not is_admin and (quote.get("customer_email") or "").lower() != email.lower():
        raise HTTPException(status_code=403, detail="That quote is not yours")

    values = (quote_documents.values_from_payment({"id": quote_id,
                                                    "amount_cents": quote.get("deposit_cents") or 0,
                                                    "customer_name": quote.get("customer_name"),
                                                    "customer_email": quote.get("customer_email"),
                                                    "reference": quote.get("quote_no")},
                                                   quote)
              if kind == "receipt" else quote_documents.values_from_quote(quote))
    try:
        data = quote_documents.render(kind, values, fmt if fmt in ("pdf", "docx") else "pdf")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    label = {"estimate": "Estimate", "bill_of_sale": "Bill-of-sale",
             "receipt": "Receipt"}.get(kind, kind)
    ext = "docx" if fmt == "docx" else "pdf"
    return Response(
        content=data,
        media_type=("application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document" if ext == "docx" else "application/pdf"),
        headers={"Content-Disposition":
                 f'attachment; filename="{quote.get("quote_no") or quote_id}-{kind}.{ext}"'})


@app.get("/materials/suppliers", response_class=HTMLResponse)
async def materials_suppliers(request: Request, db=Depends(get_db)):
    """What can be pulled right now, and what is waiting on credentials.

    Exists because every supplier client in this tree is correct but has no
    trigger: without a reachable entry point a working adapter is just code
    that looks wired.
    """
    require_admin(request)
    supplier_prices.ensure_schema(db)
    # A blank table reads as a broken page, so put the known yards in the
    # first time somebody looks. Seeding writes supplier records, never
    # prices -- a seeded yard still says "0 items" until a real price lands.
    try:
        supplier_prices.seed_default_suppliers(db)
    except Exception as e:
        print(f"[suppliers] seed skipped: {e}")
    with db.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.name, s.kind, s.public_ok, s.category, s.last_synced_at,
                   s.last_sync_note,
                   (SELECT COUNT(*) FROM material_supplier_prices p
                     WHERE p.supplier_id = s.id) AS price_count
              FROM material_suppliers s
             WHERE s.company = 'construction'
          ORDER BY s.name;
        """)
        rows = [dict(r) for r in cur.fetchall()]
    return templates.TemplateResponse(request=request, name="material_suppliers.html", context={
        "suppliers": rows,
        "prices": supplier_prices.recent_prices(db, limit=40),
        "adapters": supplier_adapters.status(),
        "estimates": materials_service.bls_escalation(),
    })


@app.post("/materials/suppliers/add")
async def materials_supplier_add(request: Request, name: str = Form(""),
                                 category: str = Form(""), default_uom: str = Form("each"),
                                 public_ok: str = Form(""), feed_url: str = Form(""),
                                 feed_auth_header: str = Form(""), feed_token: str = Form(""),
                                 sheet_text: str = Form(""),
                                 sheet_file: UploadFile | None = File(None),
                                 db=Depends(get_db)):
    """Register a supplier and load its prices in one step.

    This is the form that was missing. supplier_prices could parse a yard's
    CSV/TSV export and publish it to the customer picker since the module was
    written, but nothing on any page reached register_sheet_supplier() -- the
    only UI was a Sync button for suppliers that did not exist, and the help
    text pointed at /materials/import, which writes materials_catalog (the
    phone bot's table), not material_supplier_prices (the picker's). So the
    public picker stayed empty no matter what was pasted where.

    Two ways in: a pasted or uploaded price sheet (kind=manual, loaded now),
    or a feed URL that returns CSV (kind=feed, fetched now and on every Sync).
    """
    require_admin(request)
    supplier_prices.ensure_schema(db)
    name = (name or "").strip()
    if not name:
        return RedirectResponse(url="/materials/suppliers?error=supplier name is required",
                                status_code=status.HTTP_303_SEE_OTHER)
    text = (sheet_text or "").strip()
    if sheet_file is not None and getattr(sheet_file, "filename", ""):
        try:
            raw = await sheet_file.read()
            text = raw.decode("utf-8-sig", errors="replace").strip() or text
        except Exception as e:
            return RedirectResponse(url=f"/materials/suppliers?error=could not read file: {str(e)[:120]}",
                                    status_code=status.HTTP_303_SEE_OTHER)
    is_public = (public_ok or "").lower() in ("1", "on", "true", "yes")
    try:
        if text:
            res = supplier_adapters.register_sheet_supplier(
                db, name, text, category=category, default_uom=default_uom or "each",
                public_ok=is_public)
        elif (feed_url or "").strip():
            sid = supplier_prices.upsert_supplier(
                db, name, kind="feed", feed_url=feed_url, default_uom=default_uom or "each",
                feed_auth_header=feed_auth_header, feed_token=feed_token,
                public_ok=is_public, category=category)
            res = supplier_prices.sync_supplier(db, sid)
        else:
            return RedirectResponse(
                url="/materials/suppliers?error=paste or upload a price sheet, or give a feed URL",
                status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url=f"/materials/suppliers?error={str(e)[:200]}",
                                status_code=status.HTTP_303_SEE_OTHER)
    loaded = res.get("priced", res.get("rows", 0))
    notice = f"{name}: {loaded} priced item(s) loaded"
    if res.get("unpriced"):
        notice += f", {res['unpriced']} without a price"
    if is_public and loaded:
        notice += " — now live on /materials-picker"
    return RedirectResponse(url=f"/materials/suppliers?notice={notice}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/suppliers/sync")
async def materials_supplier_sync(request: Request, supplier_id: int = Form(0),
                                  db=Depends(get_db)):
    """Run one supplier's sync. The trigger supplier_prices.sync_supplier needed."""
    require_admin(request)
    if not supplier_id:
        return RedirectResponse(url="/materials/suppliers?error=no supplier selected",
                                status_code=status.HTTP_303_SEE_OTHER)
    try:
        res = supplier_prices.sync_supplier(db, supplier_id)
        notice = f"{res['supplier']}: {res.get('priced', res.get('rows', 0))} priced"
        if res.get("unpriced"):
            notice += f", {res['unpriced']} could not be priced"
        return RedirectResponse(url=f"/materials/suppliers?notice={notice}",
                                status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        return RedirectResponse(url=f"/materials/suppliers?error={str(e)[:200]}",
                                status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/suppliers/price")
async def materials_supplier_price(request: Request, supplier_id: int = Form(0),
                                   item_number: str = Form(""),
                                   description: str = Form(""),
                                   uom: str = Form("each"),
                                   price: str = Form(""),
                                   db=Depends(get_db)):
    """Record one price by hand -- a receipt, a quote, a shelf tag.

    This is the path that needs no credential, and it is the only one that
    can produce a price today: every supplier API in this tree wants a
    partner key the business does not have, the yards publish no feed, and
    Home Depot answers an unauthenticated product fetch with 403. A receipt
    is a real observation with a real date, which is what the table stores.
    """
    require_admin(request)
    supplier_prices.ensure_schema(db)
    if not supplier_id:
        return RedirectResponse(url="/materials/suppliers?error=pick a supplier first",
                                status_code=status.HTTP_303_SEE_OTHER)
    raw = (price or "").strip().replace("$", "").replace(",", "")
    try:
        if not raw:
            raise ValueError("a price is required")
        try:
            cents = int(round(float(raw) * 100))
        except ValueError:
            raise ValueError(f"'{price.strip()}' is not a price — enter a number like 4.28")
        supplier_prices.add_price(db, supplier_id, item_number=item_number,
                                  description=description, uom=uom, price_cents=cents)
    except Exception as e:
        return RedirectResponse(url=f"/materials/suppliers?error={str(e)[:200]}",
                                status_code=status.HTTP_303_SEE_OTHER)
    label = (item_number or description or "item").strip()
    return RedirectResponse(
        url=f"/materials/suppliers?notice={label}: ${cents / 100:,.2f} recorded",
        status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/suppliers/price/delete")
async def materials_supplier_price_delete(request: Request, price_id: int = Form(0),
                                          db=Depends(get_db)):
    """Remove a price entered against the wrong item -- the usual typo."""
    require_admin(request)
    if not price_id or not supplier_prices.delete_price(db, price_id):
        return RedirectResponse(url="/materials/suppliers?error=price not found",
                                status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/materials/suppliers?notice=price removed",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/suppliers/delete")
async def materials_supplier_delete(request: Request, supplier_id: int = Form(0),
                                    db=Depends(get_db)):
    """Drop a supplier and every price loaded against it."""
    require_admin(request)
    if not supplier_id or not supplier_prices.delete_supplier(db, supplier_id):
        return RedirectResponse(url="/materials/suppliers?error=supplier not found",
                                status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/materials/suppliers?notice=supplier removed",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/save")
async def materials_save(
    request: Request,
    item_id: int = Form(0),
    name: str = Form(""),
    price: str = Form(""),
    store: str = Form(""),
    brand: str = Form(""),
    category: str = Form(""),
    model: str = Form(""),
    sku: str = Form(""),
    unit: str = Form("each"),
    notes: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    res = materials_service.upsert_catalog_item(
        db, name=name, price_dollars=price, store=store, brand=brand,
        category=category, model=model, sku=sku, unit=unit, notes=notes,
        item_id=item_id or None)
    if res.get("ok"):
        return RedirectResponse(url="/materials?notice=Saved", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url=f"/materials?error={res.get('error','could not save')}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/import")
async def materials_import(request: Request, text: str = Form(""),
                           default_store: str = Form(""), db=Depends(get_db)):
    """Bulk paste. A single bad line rejects the whole batch -- see
    materials_service.bulk_import_catalog for why it is all-or-nothing."""
    require_admin(request)
    res = materials_service.bulk_import_catalog(db, text, default_store=default_store)
    if res.get("imported"):
        notice = f"Imported {res['imported']} item(s)"
        if res.get("skipped"):
            notice += f", skipped {res['skipped']}"
        return RedirectResponse(url=f"/materials?notice={notice}",
                                status_code=status.HTTP_303_SEE_OTHER)
    err = "; ".join(res.get("errors") or ["nothing imported"]) or "nothing imported"
    return RedirectResponse(url=f"/materials?error={err}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/materials/delete")
async def materials_delete(request: Request, item_id: int = Form(0),
                           db=Depends(get_db)):
    """Disable, never delete -- a wrong price should not erase the fact that
    the item was quoted."""
    require_admin(request)
    materials_service.delete_catalog_item(db, item_id)
    return RedirectResponse(url="/materials?notice=Removed", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/inventory/update")
async def inventory_update(
    request: Request,
    item_id: int = Form(0),
    item_name: str = Form(""),
    company: str = Form("construction"),
    delta: str = Form(""),
    quantity: str = Form(""),
    reorder_level: str = Form(""),
    category: str = Form(""),
    db=Depends(get_db),
):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    co = "construction"
    try:
        change = float(delta or quantity or "0")
    except ValueError:
        change = 0.0
    try:
        reorder_f = float(reorder_level) if reorder_level else None
    except ValueError:
        reorder_f = None
    with db.cursor() as cur:
        if item_id:
            cur.execute("SELECT * FROM supplies_inventory WHERE id = %s;", (item_id,))
            row = cur.fetchone()
        elif item_name.strip():
            cur.execute("SELECT * FROM supplies_inventory WHERE company = %s AND item_name ILIKE %s;", (co, item_name.strip()))
            row = cur.fetchone()
            if not row:
                cur.execute(
                    "INSERT INTO supplies_inventory (company, item_name, category, reorder_level) VALUES (%s, %s, %s, %s) RETURNING *;",
                    (co, item_name.strip(), category or "general", reorder_f or 0),
                )
                row = cur.fetchone()
        else:
            row = None
        if not row:
            return RedirectResponse(url=f"/inventory?company={co}", status_code=303)
        new_qty = float(row.get("qty_on_hand") or 0) + change
        if reorder_f is not None:
            cur.execute(
                "UPDATE supplies_inventory SET qty_on_hand = %s, reorder_level = %s, category = COALESCE(NULLIF(%s,''), category), updated_at = NOW() WHERE id = %s;",
                (new_qty, reorder_f, category or "", row["id"]),
            )
        else:
            cur.execute("UPDATE supplies_inventory SET qty_on_hand = %s, updated_at = NOW() WHERE id = %s;", (new_qty, row["id"]))
        db.commit()
    return RedirectResponse(url=f"/inventory?company={co}", status_code=303)


@app.post("/api/leads/{lead_id}/status")
async def update_lead_status_route(lead_id: int, request: Request, status_val: str = Form("new"), db=Depends(get_db)):
    require_admin(request)
    stage = lead_lifecycle.to_stage(status_val)
    if stage not in lead_lifecycle.STAGES:
        raise HTTPException(status_code=400, detail="Unknown status")
    store = lead_store.get_lead(db, lead_id, company="construction")
    if not store:
        raise HTTPException(status_code=404, detail="No such lead")
    with db.cursor() as cur:
        allowed = lead_store._columns(cur, "leads")
        if "status" in allowed:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (stage, lead_id))
            db.commit()
            try:
                zoho_sync.sync_status(db, lead_id)
            except Exception as e:
                print(f"⚠️ zoho status sync failed for {lead_id}: {e}", flush=True)
        db.commit()
    lead_store.recompute(db, lead_id)
    return RedirectResponse(url=request.headers.get("referer") or "/leads", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/quotes/{quote_id}/accept")
async def accept_quote(quote_id: int, request: Request, db=Depends(get_db)):
    """Accept a quote and trigger project creation + payment flow."""
    require_admin(request)
    try:
        quoting.ensure_schema(db)
        quote_result = quoting.set_status(db, quote_id, quoting.STATUS_ACCEPTED)
        if not quote_result.get("ok"):
            raise HTTPException(status_code=400, detail=quote_result.get("error"))
        
        # CASCADE: Quote accepted → Project created
        project_result = lead_pipeline.on_quote_accepted(db, quote_id)
        if project_result.get("project_created"):
            print(f"✅ Quote {quote_id} accepted → Project {project_result['project_id']} created", flush=True)
        
        return JSONResponse(content={"status": "success", "quote_id": quote_id, "project_id": project_result.get("project_id")})
    except Exception as e:
        logger.error(f"Quote accept failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/bids/{bid_id}/won")
async def mark_bid_won(bid_id: int, request: Request, db=Depends(get_db)):
    """Mark a bid won and trigger project creation."""
    require_admin(request)
    try:
        import bid_opportunities
        bid_opportunities.set_status(db, bid_id, "won", "construction")
        
        # CASCADE: Bid won → Project created. (This route 500'd on every
        # click: project_result was never assigned.)
        project_result = lead_pipeline.on_bid_won(db, bid_id) or {}
        if project_result.get("project_created"):
            print(f"✅ Bid {bid_id} won → Project {project_result['project_id']} created", flush=True)
        
        return JSONResponse(content={"status": "success", "bid_id": bid_id, "project_id": project_result.get("project_id")})
    except Exception as e:
        logger.error(f"Bid won failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/leads/{lead_id}/pipeline-trigger")
async def trigger_lead_pipeline(lead_id: int, request: Request, db=Depends(get_db)):
    """Manually trigger pipeline for a single lead (enrich → contact → quote)."""
    require_admin(request)
    try:
        result = await asyncio.to_thread(lead_pipeline.on_lead_created, db, lead_id)
        return JSONResponse(content={"status": "success", "cascaded": True})
    except Exception as e:
        logger.error(f"Pipeline trigger failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# --- Pipeline board ---------------------------------------------------------
# One visual board across the three streams the owner actually works:
# contactable leads, address-only permits, and backlog jobs. Each stream keeps
# its own status vocabulary (a permit has no phone, so it can never be "quoted"
# in the lead sense), so the board renders one column set per stream.

# Board rendering limits. The previous code used a single `LIMIT 400` across
# all columns plus a `cards[:60]` slice, so 1,516 permits and 417 leads were
# absent with nothing on the page saying so. Cards are now fetched PER STATUS
# and the true per-status totals are counted separately, so a column header can
# honestly read "1,846" while showing 60 and offering a link to the rest.
CARD_FETCH_PER_STATUS = 80
CARD_RENDER_CAP = 60

PIPELINE_STREAMS = ("leads", "permits", "backlog")


@app.get("/pipeline", response_class=HTMLResponse)
async def pipeline_page(request: Request, db=Depends(get_db)):
    """The one page that answers: who do I call, and what is it worth?

    Replaces a 3-stream x 7-column kanban. That board was impressive and
    unusable: every permit row in the county appeared as a card, most of them
    had no contact, and a "won" column was populated by fabricated demo jobs.
    The shared lead lifecycle already knows the single next action for every
    lead, so this page is built around that instead of around column-dragging.

    Nothing here writes. It reads the queue, the canonical stages, and the
    permit counts, and links into the existing work surfaces.
    """
    # Redirect to /login like the other owner pages. require_admin() raises a
    # bare 401, which showed the signed-out user a dead end instead of a login.
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    vis = source_visibility_clause()
    vis_where = vis[len(" AND "):] if vis.startswith(" AND ") else vis.strip()
    vis_params = tuple(source_visibility_params())

    queue_where = "source <> 'backlog'"
    if vis_where:
        queue_where += " AND " + vis_where
    rows = lead_store.list_leads(
        db, "construction", limit=1000, include_closed=False,
        where_extra=queue_where, extra_params=vis_params,
    )
    buckets = lead_queue.group(rows)
    by_action = {b["action"]: b for b in buckets}
    action_counts = lead_queue.action_counts(buckets)

    def _real_value_cents(lead) -> int:
        """The lead's dollar value, but only when it is not an assumed ballpark.

        48 permit leads carry a whole-home quote built from an *assumed* 1,750
        sqft. Every one of them is exactly $306,250, so the call list showed the
        same invented number twelve times and "Open pipeline" summed them into
        $49M. A ballpark with a guessed square footage is not a pipeline value.
        The permit's own declared job value has no `estimate_json`, so it still
        shows.
        """
        cents = int(lead.get("estimate_high_cents")
                    or lead.get("estimate_low_cents") or 0)
        if not cents:
            return 0
        raw = lead.get("estimate_json")
        if raw:
            try:
                meta = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                meta = {}
            if isinstance(meta, dict) and meta.get("assumed_sqft"):
                return 0
        return cents

    # The call list. Phone first because a phone call is the only channel that
    # reliably closes a remodel; email-only leads follow, marked as such.
    call_list = []
    seen_ids = set()
    for action in (lead_lifecycle.ACTION_RESPOND,
                   lead_lifecycle.ACTION_FOLLOW_UP,
                   lead_lifecycle.ACTION_FIRST_TOUCH):
        for lead in (by_action.get(action) or {}).get("leads", []):
            if lead["id"] in seen_ids:
                continue
            phone = (lead.get("phone") or "").strip()
            email = (lead.get("email") or "").strip()
            if email.lower().endswith("@lead.local"):
                email = ""
            if not phone and not email:
                continue
            seen_ids.add(lead["id"])
            call_list.append({
                "id": lead["id"],
                "name": lead.get("name") or "(no name)",
                "address": lead.get("address") or "",
                "project_type": lead.get("project_type") or "",
                "source": lead.get("source") or "",
                "phone": phone,
                "email": email,
                "action": action,
                "action_label": lead_lifecycle.ACTION_LABELS.get(action, action),
                "value_cents": _real_value_cents(lead),
            })
            if len(call_list) >= 12:
                break
        if len(call_list) >= 12:
            break

    # Canonical stage funnel. Raw status values still include the legacy
    # vocabulary (quoted/deposit/in_progress/completed), so translate rather
    # than pretending the column is clean.
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, count(*)::int AS c FROM leads "
            "WHERE company = 'construction' AND source <> 'backlog'" + vis +
            " GROUP BY status;",
            vis_params,
        )
        stage_counts = {stage: 0 for stage in lead_lifecycle.STAGES}
        for r in cur.fetchall():
            stage_counts[lead_lifecycle.to_stage(r["status"])] += int(r["c"])

        # "Work in play" is money attached to a lead we have actually spoken to,
        # and only real values: a whole-home ballpark from an assumed square
        # footage is excluded above, and untouched permit supply is not a deal
        # yet. Summing every open estimate produced $49M of fiction.
        cur.execute(
            "SELECT estimate_low_cents, estimate_high_cents, estimate_json "
            "FROM leads WHERE company = 'construction'"
            " AND status NOT IN ('new', 'won', 'lost', 'cancelled', 'canceled', 'closed', 'archived')"
            " AND source <> 'backlog'" + vis + ";",
            vis_params,
        )
        work_in_play_cents = sum(_real_value_cents(r) for r in cur.fetchall())

        cur.execute(
            "SELECT count(*)::int AS c FROM comms_logs "
            "WHERE direction = 'outbound' AND created_at >= NOW() - INTERVAL '7 days';"
        )
        touches_this_week = int(cur.fetchone()["c"] or 0)

        # Supply, by source, so the page says where the work is coming from.
        cur.execute(
            "SELECT COALESCE(NULLIF(source, ''), 'unknown') AS source, count(*)::int AS c, "
            "count(*) FILTER (WHERE COALESCE(phone, '') <> '' "
            "  OR (COALESCE(email, '') <> '' AND email NOT LIKE '%%@lead.local'))::int AS reachable "
            "FROM leads WHERE company = 'construction' AND status <> ALL(%s)" + vis +
            " GROUP BY 1 ORDER BY c DESC;",
            (list(lead_lifecycle.CLOSED_STAGES), *vis_params),
        )
        sources = cur.fetchall()

        pstats = permit_stats.permit_stats(cur)

    stage_rows = []
    max_stage = max(stage_counts.values()) or 1
    for stage in lead_lifecycle.STAGES:
        count = stage_counts.get(stage, 0)
        stage_rows.append({
            "key": stage,
            "label": lead_lifecycle.STAGE_LABELS[stage],
            "count": count,
            "pct": round(count / max_stage * 100),
        })

    # The rest of the home screen: money (any method), the few things only a
    # person can do, and what the bot did while nobody was looking. Each
    # helper swallows its own failure so one missing table cannot blank the
    # page.
    today.ensure_schema(db)
    money = today.money_this_month(db)
    health = {
        "openai": settings_status.openai_health(),
        "email": settings_status.email_status(_mail_status_safe()),
        "phone": settings_status.phone_status(vapi.is_configured(), signalwire.is_configured()),
        "jobs": settings_status.background_jobs(),
        "review_mode": auto_reply._review_mode(),
    }
    return templates.TemplateResponse(request=request, name="today.html", context={
        "user": {"email": user_email},
        "saved": request.query_params.get("saved", ""),
        "error": request.query_params.get("error", ""),
        "call_list": call_list,
        "action_counts": action_counts,
        "action_labels": lead_lifecycle.ACTION_LABELS,
        "stage_rows": stage_rows,
        "work_in_play_cents": work_in_play_cents,
        "touches_this_week": touches_this_week,
        "sources": sources,
        "permits": pstats,
        "permit_status": _permit_status(),
        "money": money,
        "deposits_pending": today.deposits_pending(db),
        "quotes_out": today.quotes_out(db),
        "bids_due": today.bids_due(db),
        "jobs_week": today.jobs_this_week(db),
        "activity": today.bot_activity(db),
        "health": health,
        "payment_methods": today.PAYMENT_METHODS,
        "payment_kinds": today.PAYMENT_KINDS,
        "today": date.today(),
    })


@app.post("/api/payments/record")
async def payments_record(request: Request, lead_id: str = Form(""), amount: str = Form(""),
                          method: str = Form("cash"), kind: str = Form("deposit"),
                          memo: str = Form(""), received_on: str = Form(""),
                          db=Depends(get_db)):
    """A customer paid by cash / check / Zelle / Venmo / ACH / card.

    The owner rarely uses Stripe; until now the only way a payment reached
    the books was the Stripe webhook, so most of the money the business took
    in never appeared anywhere. One form: payments row, ledger entry, lead
    marked paid/won.
    """
    require_admin(request)
    try:
        cents = int(round(float((amount or "0").replace("$", "").replace(",", "")) * 100))
    except ValueError:
        return RedirectResponse(url="/pipeline?error=Enter+an+amount+like+1500", status_code=status.HTTP_303_SEE_OTHER)
    lid = int(lead_id) if (lead_id or "").strip().isdigit() else None
    try:
        when = date.fromisoformat(received_on) if received_on.strip() else None
    except ValueError:
        when = None
    actor = current_actor(request) or {}
    try:
        res = today.record_payment(db, lead_id=lid, amount_cents=cents, method=method, kind=kind,
                                   memo=memo, received_on=when, recorded_by=str(actor.get("email") or ""))
    except ValueError as e:
        return RedirectResponse(url=f"/pipeline?error={urllib.parse.quote(str(e))}", status_code=status.HTTP_303_SEE_OTHER)
    note = f"Recorded {_money(cents)} {method} {kind}"
    if not (res.get("ledger") or {}).get("posted", True):
        note += " (books: " + str((res.get("ledger") or {}).get("reason", "not posted"))[:80] + ")"
    return RedirectResponse(url=f"/pipeline?saved={urllib.parse.quote(note)}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/pipeline/move")
async def pipeline_move(
    request: Request,
    stream: str = Form(...),
    item_id: int = Form(...),
    status: str = Form(...),
    db=Depends(get_db),
):
    """Move a card between columns. Used by the board's drag/drop."""
    require_admin(request)
    if stream == "permits":
        if status not in PERMIT_STATUSES:
            raise HTTPException(status_code=400, detail="Unknown permit status")
        with db.cursor() as cur:
            cur.execute("UPDATE job_leads SET status = %s WHERE id = %s;", (status, item_id))
    elif stream == "backlog":
        if status not in LEAD_STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status")
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s AND source = 'backlog' AND company = 'construction';",
                        (status, item_id))
    elif stream == "leads":
        if status not in LEAD_STATUSES:
            raise HTTPException(status_code=400, detail="Unknown status")
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s AND company = 'construction';",
                        (status, item_id))
    else:
        raise HTTPException(status_code=400, detail="Unknown stream")
    db.commit()
    return JSONResponse(content={"ok": True})


@app.post("/api/skiptrace")
async def api_skiptrace(request: Request, street: str = Form(""), city: str = Form(""),
                       state: str = Form(""), zip_code: str = Form(""),
                       db=Depends(get_db)):
    """Look up the owner of record for an address. Admin-only, cache-first.

    Cache-first is not an optimisation here, it is the whole design: RentCast's
    free plan allows 50 calls per MONTH, so every repeat lookup -- including a
    repeat miss -- has to come out of the database rather than the provider.

    POST rather than GET so the address never lands in access logs, browser
    history, or a Referer header. It is homeowner PII either way.
    """
    require_admin(request)
    _, user_email = require_auth(request)

    # trace_address blocks on urllib for up to 20s. In an async route that stalls
    # the whole event loop, so it goes to a worker thread. The DB work stays on
    # this thread: psycopg connections are not safe to share across threads.
    key = skiptrace_service.normalize_address(street, city, state, zip_code)
    if not key:
        raise HTTPException(status_code=400, detail="street and city are required")

    with db.cursor() as cur:
        skiptrace_service.ensure_schema(cur)

    cached = None
    with db.cursor() as cur:
        cached = skiptrace_service.cache_get(cur, key)
    if cached is not None and (
        cached.get("found") or cached.get("_cache_source") == "hrgeo"
    ):
        cached["found"] = bool(cached.get("found"))
        cached["contact_lookup_available"] = skip_sherpa_service.configured()
        cached["pdl_contact_available"] = pdl_contact_service.configured()
        with db.cursor() as cur:
            skiptrace_service.audit(cur, user_email, key, cached, cached=True)
            cached["known_contacts"] = _known_contacts_for(cur, street, city, state, zip_code)
        db.commit()
        return JSONResponse(content=cached)

    try:
        result = await asyncio.to_thread(
            skiptrace_service.trace_address, street, city, state, zip_code)
    except skiptrace_service.ProviderError as exc:
        logger.warning("Owner lookup provider failure status=%s detail=%s", exc.status, exc.message)
        db.rollback()
        return JSONResponse(status_code=exc.status, content={
            "ok": False, "error": "provider_error", "detail": exc.message,
        })

    with db.cursor() as cur:
        skiptrace_service.cache_put(cur, key, result)
        skiptrace_service.audit(cur, user_email, key, result, cached=False)
        result["known_contacts"] = _known_contacts_for(cur, street, city, state, zip_code)
    result["contact_lookup_available"] = skip_sherpa_service.configured()
    result["pdl_contact_available"] = pdl_contact_service.configured()
    db.commit()
    return JSONResponse(content=result)


@app.post("/api/leads/{lead_id}/notes")
async def update_lead_notes(lead_id: int, request: Request, notes: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET notes = %s WHERE id = %s;", (notes, lead_id))
        db.commit()
    return JSONResponse(content={"status": "success"})


@app.post("/api/leads/{lead_id}/touch")
async def record_lead_touch(lead_id: int, request: Request, channel: str = Form(""),
                            detail: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    ch = channel.strip().lower() if channel else "sms"
    if ch not in {"letter", "door-knock", "phone", "sms", "in-person", "email"}:
        ch = "sms"
    if ch == "text":
        ch = "sms"
    detail = (detail or "").strip()
    lead = lead_store.get_lead(db, lead_id, company="construction")
    if not lead:
        return JSONResponse({"ok": False, "error": "Not found"}, status_code=404)
    # Use the package's touch recorder so comms_logs and touch_count stay in
    # sync, then recompute the queue state (first_touch -> contacted/waiting).
    lead_store.record_touch(db, lead_id, ch, detail)
    lead_store.recompute(db, lead_id)
    if request.headers.get("hx-request"):
        return JSONResponse({"ok": True})
    return RedirectResponse(url=request.headers.get("referer") or "/leads",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/skiptrace/zip")
async def api_skiptrace_zip(request: Request, street: str = "", city: str = "",
                            state: str = ""):
    """Fill in the ZIP for an address, so the owner lookup stops matching city-wide.

    The page refuses a skip trace without a ZIP because a street-only match can
    resolve to the wrong house and still bill. This uses Census first and ArcGIS
    second, both free and keyless, so it keeps working now that Google geocoding
    returns REQUEST_DENIED. It never guesses: no street-level match means an
    empty zipcode and ok=false, never a city-level fallback.
    """
    require_admin(request)
    import zip_lookup

    try:
        result = zip_lookup.lookup_zip(street, city, state)
    except zip_lookup.ZipLookupError as exc:
        return JSONResponse({"ok": False, "zipcode": "", "error": str(exc)[:300]}, status_code=502)
    return JSONResponse(result)


@app.get("/api/skiptrace/addresses", response_class=HTMLResponse)
async def skiptrace_addresses(request: Request, q: str = "", db=Depends(get_db)):
    """Recent addresses to pick from, so nobody retypes a permit address.

    The per-permit button on the board already knows the address; this is the
    same list for the standalone page. Deliberately a PAGE and not JSON: it
    renders into the form via innerHTML, and every value below is escaped by
    the caller before interpolation.
    """
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, address, city, state, postal_code FROM job_leads "
            "WHERE NOT is_demo AND COALESCE(address,'') <> '' "
            "ORDER BY found_at DESC LIMIT 60;"
        )
        rows = cur.fetchall()
    term = (q or "").strip().lower()
    items = []
    for r in rows:
        parts = _permit_address_parts(r)
        if not parts["ok"]:
            continue
        addr = parts["formatted"]
        if term and term not in addr.lower():
            continue
        items.append(
            f'<option value="{html.escape(str(r["id"]))}" '
            f'data-street="{html.escape(parts["street"], quote=True)}" '
            f'data-city="{html.escape(parts["city"], quote=True)}" '
            f'data-state="{html.escape(parts["state"], quote=True)}" '
            f'data-zip="{html.escape(parts["zipcode"], quote=True)}">'
                     f'{html.escape(addr)}</option>')
    return HTMLResponse(content="".join(items) or
                        '<option value="">No addresses match</option>')


@app.post("/api/job-leads/{permit_id}/skiptrace")
async def permit_skiptrace(permit_id: int, request: Request, db=Depends(get_db)):
    """Look up the owner of record for a permit's address. Admin-only, cache-first.

    The address is read from the permit row rather than posted by the browser.
    The permit feeds never publish an applicant name, so this is the only way a
    permit lead gets one -- and reading the address server-side keeps it out of
    the browser's hands entirely.

    Does not write to the permit. Resolving who owns the house is not the same
    as reaching them, and auto-filling contractor_name here would make an
    unverified owner look like an applied-for permit.
    """
    require_admin(request)
    _, user_email = require_auth(request)

    with db.cursor() as cur:
        cur.execute(
            "SELECT address, city, state, postal_code FROM job_leads WHERE id = %s;",
            (permit_id,),
        )
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No such permit")
    parsed = _permit_address_parts(row)
    if not parsed["ok"]:
        raise HTTPException(status_code=400,
                            detail=f"Cannot read this permit address ({parsed['why']}).")
    # parsed["zipcode"] is legitimately empty: 217 of 500 permit rows have no
    # ZIP and the provider still resolves them. The response flags that case as
    # low confidence rather than hiding it.

    key = skiptrace_service.normalize_address(parsed["street"], parsed["city"],
                                              parsed["state"], parsed["zipcode"])
    with db.cursor() as cur:
        skiptrace_service.ensure_schema(cur)
        cached = skiptrace_service.cache_get(cur, key)
    if cached is not None and (
        cached.get("found") or cached.get("_cache_source") == "hrgeo"
    ):
        cached["found"] = bool(cached.get("found"))
        cached["contact_lookup_available"] = skip_sherpa_service.configured()
        cached["pdl_contact_available"] = pdl_contact_service.configured()
        with db.cursor() as cur:
            skiptrace_service.audit(cur, user_email, key, cached, cached=True)
        db.commit()
        return JSONResponse(content=cached)

    try:
        result = await asyncio.to_thread(
            skiptrace_service.trace_address,
            parsed["street"], parsed["city"], parsed["state"], parsed["zipcode"])
    except skiptrace_service.ProviderError as exc:
        logger.warning("Owner lookup provider failure status=%s detail=%s", exc.status, exc.message)
        db.rollback()
        return JSONResponse(status_code=exc.status, content={
            "ok": False, "error": "provider_error", "detail": exc.message,
        })

    with db.cursor() as cur:
        skiptrace_service.cache_put(cur, key, result)
        skiptrace_service.audit(cur, user_email, key, result, cached=False)
    result["contact_lookup_available"] = skip_sherpa_service.configured()
    result["pdl_contact_available"] = pdl_contact_service.configured()
    db.commit()
    return JSONResponse(content=result)


@app.post("/api/skiptrace/enrich")
async def skiptrace_enrich(request: Request, company: str = Form("construction"),
                           source: str = Form("permit_finder"),
                           db=Depends(get_db)):
    """Fill in the owner name on every address-only lead, from the cache first.

    Defaults to source='permit_finder'. The first version ran unfiltered over
    every address-only lead, spent 25 of the 50 monthly calls and updated
    nothing: the loose population is mostly completed jobs and the owner's own
    STR properties, which RentCast has no record of. Pass source= for the old
    behaviour.

    Bounded per run because RentCast allows 50 calls per MONTH and that budget is
    shared with instant-quote. Repeat runs are free: a cached address is a table
    read, and a cached MISS is cached too, so a known-empty address is never
    re-spent on.
    """
    require_admin(request)

    def run(cur):
        # `source` used to be dropped here, so the Form field was accepted and
        # then ignored. Passing it through is what the signature and the
        # docstring always described.
        return skiptrace_service.enrich_address_only_leads(cur, company, source=source)

    summary = await asyncio.to_thread(run_with_cursor, db, run)
    db.commit()
    return JSONResponse(content=summary)


def _zoho_mail_status() -> dict:
    """What is actually delivering mail right now, and what is missing.

    Outbound has three possible paths, in the order documents_service tries
    them: the Zoho Mail REST API (needs the mail OAuth scope), the Gmail API
    (works today), then SMTP (unreachable -- Railway blocks 25/465/587).
    Inbound is separate: replies land in Zoho because that is where MX points,
    and only IMAP or the Mail API can read them.
    """
    transport = (os.getenv("EMAIL_TRANSPORT") or "gmail").strip().lower()
    api = False
    try:
        import zoho_mail
        api = bool(zoho_mail.available())
    except Exception:
        api = False
    inbox: dict = {"ok": False, "reason": "", "mailbox": ""}
    if api:
        try:
            inbox = zoho_mail.inbox_status()
        except Exception as exc:
            inbox = {"ok": False, "reason": str(exc)[:120], "mailbox": ""}
    if not inbox.get("ok"):
        try:
            import zoho_imap
            imap = zoho_imap.status()
            if imap.get("ok") or not inbox.get("reason"):
                inbox = imap
        except Exception as exc:
            inbox = {"ok": False, "reason": str(exc)[:120], "mailbox": ""}
    sending = "Zoho Mail API" if api else ("Gmail API" if transport in ("gmail", "auto") else "SMTP")
    return {
        "transport": transport,
        "sending": sending,
        "api_ready": api,
        "imap_ok": bool(inbox.get("ok")),
        "imap_via": inbox.get("via") or "IMAP",
        "imap_reason": inbox.get("reason") or "",
        "mailbox": inbox.get("mailbox") or (os.getenv("SMTP_FROM") or ""),
        "smtp_from": os.getenv("SMTP_FROM") or "",
    }


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    getter = lambda key: _get_setting(db, key)
    configured = resimply_service.configured(getter)
    resimply_status = None
    if configured:
        resimply_status = await asyncio.to_thread(resimply_service.verify, getter)
    # Status probes run off the loop: the OpenAI check is a real (cached)
    # request, and the mail preflight may refresh a token.
    openai_health, mail = await asyncio.gather(
        asyncio.to_thread(settings_status.openai_health),
        asyncio.to_thread(_mail_status_safe),
    )
    is_authed, user_email = require_auth(request)
    otp_env = (os.getenv("OTP_ENABLED") or "").strip().lower()
    return templates.TemplateResponse(
        request=request,
        name="settings.html",
        context={
            "user": {"email": user_email},
            "saved": request.query_params.get("saved", ""),
            "skip_sherpa_configured": skip_sherpa_service.configured(),
            "resimply_configured": configured,
            "resimply_status": resimply_status,
            "resimply_missing": resimply_service.missing_ids(getter),
            "openai": openai_health,
            "email": settings_status.email_status(mail),
            "phone": settings_status.phone_status(vapi.is_configured(), signalwire.is_configured()),
            "payments": settings_status.payments_status(stripe_svc.is_configured(), default_deposit_cents()),
            "jobs": settings_status.background_jobs(),
            "guardrails": settings_status.outbound_guardrails(),
            "otp_on": _otp_enabled(db),
            "otp_env_locked": otp_env in ("0", "false", "no", "off", "1", "true", "yes", "on"),
            "zoho": zoho_oauth.status(),
            "zoho_scope_string": ",".join(zoho_oauth.SCOPES),
            "zoho_mail_status": _zoho_mail_status(),
            "zoho_lists": await asyncio.to_thread(zoho_campaigns.mailing_lists),
            "zoho_list_key": zoho_campaigns.list_key(),
        },
    )


def _mail_status_safe() -> dict:
    try:
        import loan_outreach
        return loan_outreach.mail_status()
    except Exception as exc:
        return {"ok": False, "detail": f"preflight failed: {exc}", "checked_at": ""}


@app.post("/api/settings/security")
async def settings_security(request: Request, otp_enabled: str = Form(""), db=Depends(get_db)):
    """Master switch for emailed sign-in codes on the owner login.

    Stored in app_settings; the OTP_ENABLED environment variable, when set,
    overrides it (so a deploy can force it either way). Only the master switch
    is exposed: per-role toggles would be buttons nothing reads yet.
    """
    require_admin(request)
    _set_setting(db, "otp_enabled", "true" if otp_enabled.lower() in ("on", "1", "true", "yes") else "false")
    return RedirectResponse(url="/settings?saved=security#security", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/settings/resimply")
async def save_resimply_settings(request: Request, db=Depends(get_db)):
    require_admin(request)
    form = await request.form()
    fields = {
        "resimply_api_token": resimply_service.TOKEN_SETTING,
        "resimply_campaign_id": "resimply_campaign_id",
        "resimply_market_id": "resimply_market_id",
        "resimply_pipeline_id": "resimply_pipeline_id",
        "resimply_status_id": "resimply_status_id",
    }
    for field, key in fields.items():
        value = (form.get(field) or "").strip()
        if value:
            _set_setting(db, key, value)
    if form.get("clear_resimply"):
        with db.cursor() as cur:
            cur.execute("DELETE FROM app_settings WHERE key = ANY(%s);",
                        (list(fields.values()),))
            db.commit()
    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


def _actor_label(request: Request) -> str:
    """Who to record in an audit row. Best-effort; never raises.

    The audit trail is what makes holding homeowner contact details defensible,
    so the actor has to be recorded even when the session carries no email. The
    fallback is the request IP -- real but less useful -- rather than a blank
    or a fabricated identity.
    """
    # require_admin already proved an admin session exists, so an email is
    # normally available here. The cookie is the fallback for the older admin
    # path; the IP is the last resort.
    try:
        actor = current_actor(request)
        if actor and actor.get("email"):
            return str(actor["email"])[:255]
        legacy = request.cookies.get("user_email")
        if legacy:
            return str(legacy)[:255]
    except Exception:
        pass
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    return f"ip:{host}" if host else "unknown"


@app.get("/api/skiptrace/pdl-status")
async def skiptrace_pdl_status(request: Request):
    """Is the PDL contact layer usable? Admin-only, and spends nothing.

    The page needs this to decide whether to offer the button at all, so a
    misconfigured key shows as "not available" rather than a failed click.
    """
    require_admin(request)
    return JSONResponse(content={
        "pdl_configured": pdl_contact_service.configured(),
        "rentcast_configured": property_service.is_configured(),
        "skip_sherpa_configured": skip_sherpa_service.configured(),
    })


@app.post("/api/skiptrace/pdl-contact")
async def skiptrace_pdl_contact(request: Request, street: str = Form(""),
                                city: str = Form(""), state: str = Form(""),
                                zip_code: str = Form(""), permit_id: str = Form(""),
                                db=Depends(get_db)):
    """Email/phone for one address via PDL, after an explicit click.

    The counterpart to /api/skiptrace/contact. That route bills Skip Sherpa per
    lookup; this one uses the PDL key that is already configured, which is what
    the parcel lookup cannot supply and what commit c4a77ae removed by accident.

    One address per click, no batch path: PDL bills per person search. A ZIP is
    required, because without one PDL can match at city level and hand back a
    neighbour's contact details.
    """
    require_admin(request)
    if permit_id:
        try:
            selected_id = int(permit_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid permit selection")
        with db.cursor() as cur:
            cur.execute(
                "SELECT address, city, state, postal_code FROM job_leads "
                "WHERE id = %s AND NOT is_demo;",
                (selected_id,),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No such permit")
        parsed = _permit_address_parts(row)
        if not parsed["ok"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot read that permit's address ({parsed['why']}).",
            )
        street, city = parsed["street"], parsed["city"]
        state, zip_code = parsed["state"], parsed["zipcode"]

    if not pdl_contact_service.configured():
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "pdl_not_configured",
            "detail": ("Email/phone lookup needs PDL_API_KEY. Owner names are still "
                       "available from free public parcels. No lookup was made."),
        })

    def run(cur):
        # The owner name is free (HRGEO/RentCast) and tells a human whose door
        # this is before spending a PDL search on it. Best-effort: if the parcel
        # layer is down the PDL search is still worth making.
        owner_name = ""
        try:
            trace = skiptrace_service.trace_address(
                street.strip(), city.strip(), state.strip().upper(), zip_code.strip()
            )
            if trace.get("found"):
                owner_name = trace.get("owner_of_record") or ""
        except Exception:
            owner_name = ""

        result = pdl_contact_service.lookup(
            street.strip(), city.strip(), state.strip().upper(), zip_code.strip(),
            owner_name=owner_name, cur=cur,
            requested_by=_actor_label(request),
        )
        return result

    try:
        result = await asyncio.to_thread(lambda: run_with_cursor(db, run))
    except Exception as exc:
        logger.warning("PDL contact lookup failed: %s", exc)
        db.rollback()
        return JSONResponse(status_code=502, content={
            "ok": False, "error": "pdl_lookup_failed", "detail": str(exc)[:300],
        })
    db.commit()
    return JSONResponse(content=result)


@app.post("/api/skiptrace/contact")
async def skiptrace_contact(request: Request, street: str = Form(""),
                            city: str = Form(""), state: str = Form(""),
                            zip_code: str = Form(""), permit_id: str = Form(""),
                            db=Depends(get_db)):
    """Find contact channels only after an explicit user click."""
    require_admin(request)
    if permit_id:
        try:
            selected_id = int(permit_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid permit selection")
        with db.cursor() as cur:
            cur.execute(
                "SELECT address, city, state, postal_code FROM job_leads "
                "WHERE id = %s AND NOT is_demo;",
                (selected_id,),
            )
            row = cur.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No such permit")
        parsed = _permit_address_parts(row)
        if not parsed["ok"]:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot read that permit's address ({parsed['why']}).",
            )
        street, city = parsed["street"], parsed["city"]
        state, zip_code = parsed["state"], parsed["zipcode"]

    if not skip_sherpa_service.configured():
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "contact_lookup_not_configured",
            "detail": (
                "Owner names are available from free public parcels. Phone and email "
                "lookup needs SKIP_SHERPA_API_KEY; no contact lookup was made."
            ),
        })
    if not (street or "").strip() or not (city or "").strip():
        raise HTTPException(status_code=400, detail="Street and city are required")
    if not (zip_code or "").strip():
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "zip_required",
            "detail": (
                "Phone/email lookup needs the full address including ZIP to avoid "
                "matching the wrong property. No credit was spent."
            ),
            "credit_spent": False,
        })
    try:
        result = await asyncio.to_thread(
            skip_sherpa_service.trace,
            street.strip(), city.strip(), state.strip().upper(), zip_code.strip(),
        )
    except skip_sherpa_service.ProviderError as exc:
        logger.warning("Owner contact provider failure status=%s detail=%s", exc.status, exc.message)
        db.rollback()
        return JSONResponse(status_code=exc.status, content={
            "ok": False,
            "error": "contact_provider_error",
            "detail": exc.message,
            "credit_spent": exc.spendable,
        })
    return JSONResponse(content=result)


def _owner_draft_body(owner: str, address: str, property_data: dict) -> str:
    first = (owner or "there").split()[0]
    detail = []
    if property_data.get("year_built"):
        detail.append(f"built in {property_data['year_built']}")
    if property_data.get("square_footage"):
        detail.append(f"{int(property_data['square_footage']):,} sq ft")
    listed_property = (
        "The public assessor record lists it as " + ", ".join(detail) + ".\n\n"
        if detail else ""
    )
    return (
        f"Hi {first},\n\n"
        f"I'm reaching out about the property at {address}. I'm a local contractor "
        "and wanted to introduce myself in case you need help with a renovation "
        f"or repair.\n\n{listed_property}"
        "If this is not relevant, let me know and I won't follow up.\n\n"
        f"Thanks,\n{brand.short_name()}"
    )


def _matching_construction_lead(cur, address: str, street: str, city: str,
                                state: str, zip_code: str, source_address: str = ""):
    """Match an existing lead by parsed situs address, not display formatting."""
    number = (street.split() or [""])[0]
    if not number:
        return None
    if source_address:
        cur.execute(
            "SELECT id, name, email, phone, status, address FROM leads "
            "WHERE company = 'construction' AND status <> 'lost' "
            "AND LOWER(BTRIM(COALESCE(address,''))) = LOWER(BTRIM(%s)) "
            "ORDER BY id LIMIT 2;",
            (source_address,),
        )
        exact_matches = cur.fetchall()
        if len(exact_matches) == 1:
            return exact_matches[0]
    cur.execute(
        "SELECT id, name, email, phone, status, address FROM leads "
        "WHERE company = 'construction' AND status <> 'lost' "
        "AND address IS NOT NULL AND POSITION(%s IN LOWER(address)) > 0 "
        "ORDER BY id LIMIT 250;",
        (number.lower(),),
    )
    expected = skiptrace_service.normalize_address(street, city, state)
    expected_zip = (zip_code or "").strip()[:5]
    for lead in cur.fetchall():
        parsed = skiptrace_service.parse_address(lead.get("address") or "")
        if not parsed.get("ok"):
            continue
        candidate = skiptrace_service.normalize_address(
            parsed["street"], parsed["city"], parsed["state"]
        )
        candidate_zip = parsed.get("zipcode", "")[:5]
        if candidate == expected and (
            not expected_zip or not candidate_zip or candidate_zip == expected_zip
        ):
            return lead
    return None


@app.post("/api/skiptrace/save")
async def skiptrace_save(request: Request, owner_name: str = Form(""),
                         email: str = Form(""), phone: str = Form(""),
                         street: str = Form(""), city: str = Form(""),
                         state: str = Form(""), zip_code: str = Form(""),
                         permit_id: str = Form(""), property_json: str = Form(""),
                         push_resimply: str = Form(""),
                         db=Depends(get_db)):
    """Save reviewed owner details to a lead and stage, but never send, email."""
    require_admin(request)
    owner, email, phone = (owner_name or "").strip(), (email or "").strip(), (phone or "").strip()
    if not owner:
        raise HTTPException(status_code=400, detail="No owner name to save")
    if not email and not phone:
        raise HTTPException(status_code=400, detail="Save requires an email address or phone number")

    try:
        property_data = json.loads(property_json) if property_json else {}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Property details were malformed") from exc
    if not isinstance(property_data, dict):
        raise HTTPException(status_code=400, detail="Property details were malformed")

    source_address = ""
    address = ""
    if permit_id:
        try:
            selected_id = int(permit_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid permit selection")
        with db.cursor() as cur:
            cur.execute(
                "SELECT address, city, state, postal_code, permit_number, work_type, job_description "
                "FROM job_leads WHERE id = %s AND NOT is_demo;",
                (selected_id,),
            )
            permit = cur.fetchone()
        if not permit:
            raise HTTPException(status_code=404, detail="No such permit")
        parsed = _permit_address_parts(permit)
        if not parsed["ok"]:
            raise HTTPException(status_code=400, detail="The selected permit has no usable address")
        street, city = parsed["street"], parsed["city"]
        state, zip_code = parsed["state"], parsed["zipcode"]
        source_address = (permit.get("address") or "").strip()
        address = parsed["formatted"]
    else:
        address = ", ".join(part for part in [
            street.strip(), city.strip(),
            " ".join(value for value in [state.strip(), zip_code.strip()] if value),
        ] if part)
    parsed = skiptrace_service.parse_address(address)
    if not parsed["ok"]:
        raise HTTPException(status_code=400, detail="Enter a complete property address")

    existing = None
    with db.cursor() as cur:
        existing = _matching_construction_lead(
            cur, address, parsed["street"], parsed["city"], parsed["state"],
            parsed["zipcode"], source_address=source_address,
        )
        if existing:
            replace_name = skiptrace_service.is_placeholder_name(existing.get("name"))
            cur.execute(
                "UPDATE leads SET "
                "name = CASE WHEN %s THEN %s ELSE name END, "
                "email = CASE WHEN %s <> '' AND (BTRIM(COALESCE(email,'')) = '' "
                "OR LOWER(COALESCE(email,'')) LIKE '%%@lead.local') THEN %s ELSE email END, "
                "phone = CASE WHEN %s <> '' AND BTRIM(COALESCE(phone,'')) = '' "
                "THEN %s ELSE phone END "
                "WHERE id = %s RETURNING id, name, email, phone, status, address;",
                (replace_name, owner, email, email, phone, phone, existing["id"]),
            )
            lead = cur.fetchone()
            created = False
        else:
            description = (
                "Owner/contact details researched from the public parcel record "
                "and the selected contact lookup."
            )
            cur.execute(
                "INSERT INTO leads (name, phone, email, project_type, address, description, "
                "source, status, company) "
                "VALUES (%s, %s, %s, 'Property inquiry', %s, %s, 'permit_finder', "
                "'new', 'construction') "
                "RETURNING id, name, email, phone, status, address;",
                (owner, phone, email, address, description),
            )
            lead = cur.fetchone()
            created = True
        db.commit()

    draft_staged = False
    if (lead.get("email") or "").strip():
        draft_staged = auto_reply._stage_draft(
            db, lead["id"], "construction", "email", lead["email"],
            _owner_draft_body(owner, address, property_data),
        )

    pushed = None
    if push_resimply:
        try:
            pushed = resimply_service.push_lead(
                name=owner, email=lead.get("email") or email,
                phone=lead.get("phone") or phone, address=lead.get("address") or address,
                comment="Owner details reviewed from a public parcel record.",
                get_setting=lambda key: _get_setting(db, key),
            )
        except resimply_service.ResimplyError as exc:
            pushed = {"ok": False, "error": exc.message, "auth": exc.auth}

    return JSONResponse(content={
        "ok": True,
        "created": created,
        "lead_id": lead["id"],
        "saved": {
            "name": lead.get("name"), "email": lead.get("email"),
            "phone": lead.get("phone"), "address": lead.get("address"),
        },
        "status": lead.get("status"),
        "draft_staged": draft_staged,
        "note": (
            "Added to Leads and staged an email draft for your review. Nothing was sent."
            if created and draft_staged else
            "Contact saved to Leads. Nothing was sent."
        ),
        "resimply": pushed,
    })


@app.post("/api/permit/enrich")
async def permit_enrich_endpoint(request: Request, email: str = Form(""), db=Depends(get_db)):
    """Enrich a permit tracking email into permit details. Admin-only."""
    require_admin(request)
    result = permit_enricher.enrich((email or "").strip())
    return JSONResponse(content=result)


def run_with_cursor(db, fn):
    """Run fn(cur) on a fresh cursor and return its result.

    The trace blocks on urllib, so it cannot run on the event loop; the cursor
    is created here because a psycopg connection is not safe to share across
    threads.
    """
    with db.cursor() as cur:
        return fn(cur)


@app.post("/api/enrich/pdl")
async def enrich_pdl(request: Request, limit: int = Form(10),
                     dry_run: str = Form("1"), db=Depends(get_db)):
    """Reverse-address permit leads to owner email/phone via People Data Labs.

    DRY RUN by default. Every lookup is a billable PDL record, so the response
    reports the hit list, the miss list and the projected spend BEFORE any
    write happens. Pass dry_run=0 to actually write.

    Requires PDL_API_KEY on the service. Returns a clear 400 explaining that
    when it is absent, rather than reporting zero hits -- which would be
    indistinguishable from "PDL found nobody".
    """
    require_admin(request)
    if not enrichment.pdl_enabled():
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "PDL_API_KEY is not set on this service.",
            "how_to": "Sign up at peopledatalabs.com, then: "
                      "railway variable set PDL_API_KEY=<key> --service BizStack-Construction "
                      "--skip-deploys",
        })
    is_dry = (dry_run or "").strip().lower() not in ("0", "false", "no", "off")
    capped = max(1, min(int(limit or 10), 100))

    # enrichment opens its own psycopg connection from DATABASE_URL and blocks
    # on HTTP per record, so it cannot share this route's connection (psycopg is
    # not thread-safe) and must not run on the event loop.
    try:
        await asyncio.to_thread(enrichment.enrich_pending_leads, capped, is_dry)
    except Exception as exc:
        return JSONResponse(status_code=502, content={
            "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    if is_dry:
        summary = {"ok": True, **dict(enrichment.LAST_RUN)}
        return JSONResponse(content=summary)
    return JSONResponse(content={"ok": True, "dry_run": False, "note": "see service logs"})


@app.post("/api/email/fetch")
async def email_fetch(request: Request, cap: int = Form(0),
                      commit: str = Form(""), db=Depends(get_db)):
    """Look up a published email for named leads that have none.

    Admin only, DRY RUN unless commit=1, and off entirely unless
    EMAIL_FETCH_MAX > 0 is set as a service variable. That triple gate is
    deliberate: this walks out to public web pages, so it must be a
    conscious act, not something a stray scheduler can start.

    Returns the leads it examined, the ones it could resolve, and the ones
    with no public footprint -- the miss list matters as much as the hits.
    """
    require_admin(request)
    actor = current_actor(request) or {}
    dry = (commit or "").strip().lower() not in ("1", "true", "yes", "on")

    def run(cur):
        return email_fetcher.fetch_for_leads(
            cur, requested_by=actor.get("email") or "owner",
            cap=(cap or None), dry_run=dry,
        )

    summary = await asyncio.to_thread(run_with_cursor, db, run)
    db.commit()
    return JSONResponse(content=summary)


@app.post("/api/leads/{lead_id}/draft-email")
async def draft_email_route(lead_id: int, request: Request, db=Depends(get_db)):
    """Stage a ready-to-send draft on a lead, using its traced owner name.

    Staging only -- it does not send. Pressing send is a separate, explicit call
    to send-once, because a draft that sends itself on generation is one more
    step from the three-emails-in-thirty-seconds incident in 76d6a01.
    """
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT id, name, email, address FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail="No such lead")
    if not (lead.get("email") or "").strip():
        raise HTTPException(status_code=400,
                            detail="This lead has no email address yet. Add one before drafting.")

    owner = (lead.get("name") or "").strip() or "there"
    first = owner.split()[0] if owner else "there"
    subject = f"Hello from {brand.short_name()}"
    body = (
        f"Hi {first},\n\n"
        f"I hope this message finds you well! I'm reaching out from Buildstack "
        f"Construction, as we noticed your property at {lead.get('address') or ''}. "
        f"If you're considering any renovations or improvements, we'd love to help "
        f"you with your project.\n\n"
        f"Feel free to reply to this email or give us a call at "
        f"+1 (757) 908-7121 to discuss your ideas!\n\n"
        f"Best regards,\n"
        f"{brand.short_name()}"
    )
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET draft_reply = %s WHERE id = %s;",
                    (f"{subject}\n{body}", lead_id))
        db.commit()
    return JSONResponse(content={"ok": True, "staged": True, "lead_id": lead_id,
                                 "subject": subject, "to": lead.get("email")})


@app.post("/api/leads/{lead_id}/clear-draft")
async def clear_draft_route(lead_id: int, request: Request, db=Depends(get_db)):
    """Discard a staged draft without sending it.

    send-once stages the text into draft_reply before firing. If the send is
    blocked -- no transport, cap reached, lead already contacted -- the draft
    stays behind, and fire-all-drafts would later try to send it. That is how a
    half-finished send turns into an unexpected one. This is the manual undo.
    """
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET draft_reply = NULL WHERE id = %s RETURNING id;", (lead_id,))
        row = cur.fetchone()
        db.commit()
    if not row:
        raise HTTPException(status_code=404, detail="No such lead")
    return JSONResponse(content={"ok": True, "cleared": True, "lead_id": lead_id})


@app.post("/api/leads/{lead_id}/send-once")
async def send_once_route(lead_id: int, request: Request, subject: str = Form(""),
                          body: str = Form(""), db=Depends(get_db)):
    """Send one owner-written email to one lead. Admin-only.

    This is the "send a specific message I typed" capability that did not exist.
    fire_lead_draft could only send a draft the automated sweep had staged, and
    nothing could stage one by hand, so a hand-written message had no way out.

    It stages the text into draft_reply and then calls fire_lead_draft rather
    than sending directly. That is deliberate: every guard lives in
    fire_lead_draft -- contact_policy_allows (one touch per lead, across
    channels), _channel_allowed (daily caps), the recipient scrub, and the
    comms_logs write. Reusing it means this route cannot drift from the policy
    the rest of the bot obeys, which is exactly how the send path in 76d6a01
    came to be unguarded in the first place.

    force is NOT passed. A lead that already ignored us, or that has had its
    one timed follow-up, stays blocked here the same as anywhere else.
    """
    require_admin(request)
    subject = (subject or "").strip()
    body = (body or "").strip()
    if not subject or not body:
        raise HTTPException(status_code=400, detail="subject and body are both required")
    if len(body) > 15000:
        raise HTTPException(status_code=400, detail="body is too long")

    with db.cursor() as cur:
        cur.execute("SELECT id, name, email FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail="No such lead")
    if not (lead.get("email") or "").strip():
        raise HTTPException(status_code=400, detail="That lead has no email address")

    # draft_reply is "subject\nbody"; fire_lead_draft splits on the first
    # newline to recover the two.
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET draft_reply = %s WHERE id = %s;", (f"{subject}\n{body}", lead_id))
        db.commit()

    result = auto_reply.fire_lead_draft(db, "construction", lead_id)

    # fire_lead_draft only clears draft_reply on a real send. If it was blocked
    # -- no transport, cap reached, lead already contacted -- the text is left
    # sitting on the lead, and fire-all-drafts would later send it as if the
    # owner had staged it. Clear it here so a blocked send leaves no trace.
    if not result.get("sent"):
        try:
            with db.cursor() as cur:
                cur.execute("UPDATE leads SET draft_reply = NULL WHERE id = %s;", (lead_id,))
                db.commit()
        except Exception as exc:
            print(f"[send-once] could not clear staged draft on lead {lead_id}: {exc}", flush=True)

    return JSONResponse(content={
        "sent": bool(result.get("sent")),
        "channel": result.get("channel"),
        "why": result.get("why", ""),
        "to": lead.get("email"),
        "draft_cleared": not result.get("sent"),
    })


@app.post("/api/leads/{lead_id}/fire-draft")
async def fire_lead_draft_route(lead_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    result = auto_reply.fire_lead_draft(db, "construction", lead_id)
    if request.headers.get("hx-request"):
        return JSONResponse(content={"status": "fired" if result.get("sent") else "blocked", "why": result.get("why", "")})
    return RedirectResponse(url=request.headers.get("referer") or "/leads", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/bot/health")
async def bot_health_route(request: Request, db=Depends(get_db)):
    """Owner glance: bot activity right now as JSON (no log scrolling).

    Reports how many review drafts were sent today (email + text, honoring the
    daily caps), how many leads are still staged with a draft, and today's
    remaining cap budget per channel. Lets the owner confirm 'any leads got
    contacted' at a glance without grepping logs."""
    try:
        return auto_reply.bot_health(db)
    except Exception as exc:
        print(f"[auto-reply] bot/health failed: {exc}", flush=True)
        return JSONResponse(status_code=500, content={"error": str(exc)[:300]})


@app.post("/api/leads/fire-all-drafts")
async def fire_all_drafts_route(request: Request, db=Depends(get_db)):
    require_admin(request)
    # Scoped to this company. Without the scope this button would send every
    # staged draft on this site.
    result = auto_reply.fire_all_drafts(db, "construction")
    if request.headers.get("hx-request"):
        return JSONResponse(content=result)
    return RedirectResponse(url=request.headers.get("referer") or "/leads", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/leads/enrich-batch")
async def enrich_leads_batch(request: Request, limit: int = Form(10), db=Depends(get_db)):
    """Find phone/email for a small batch of address-only leads.

    This is the real provider path: `lead_enrich_loop` checks the free parcel
    cache first and only calls Skip Sherpa when there is no contact, so a
    repeat run over the same addresses costs nothing. The cap is deliberately
    small and clamped -- Skip Sherpa bills per lookup, and 1,700 address-only
    permits sitting in the database is exactly the kind of backlog that turns
    an uncapped button into a surprise invoice.

    Runs in a worker thread because each lookup is a network round trip; the
    request would otherwise block the event loop for every other page.
    """
    require_admin(request)
    try:
        n = max(1, min(int(limit or 10), 25))
    except (TypeError, ValueError):
        n = 10
    import lead_enrich_loop
    result = await asyncio.to_thread(lead_enrich_loop.run_once, db, True, n)
    return JSONResponse(content=result)


@app.post("/api/leads/{lead_id}/delete")
async def delete_lead(lead_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM projects WHERE lead_id = %s;", (lead_id,))
        project_ids = [r["id"] for r in cur.fetchall()]
        if project_ids:
            cur.execute("DELETE FROM job_photos WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM clock_events WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM timesheets WHERE project_id = ANY(%s);", (project_ids,))
            cur.execute("DELETE FROM projects WHERE id = ANY(%s);", (project_ids,))
        cur.execute("DELETE FROM payments WHERE lead_id = %s;", (lead_id,))
        cur.execute("DELETE FROM leads WHERE id = %s;", (lead_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/leads", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/leads/{lead_id}/deposit-link")
async def create_deposit_link_route(lead_id: int, request: Request, amount: str = "", db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
    if not lead:
        return JSONResponse(content={"error": "Lead not found"}, status_code=404)
    if lead["deposit_status"] == "paid":
        return JSONResponse(content={"error": "Deposit already paid"}, status_code=400)

    amount_cents = lead.get("deposit_cents") or default_deposit_cents()
    if amount.strip():
        try:
            amount_cents = int(float(amount) * 100)
        except ValueError:
            return JSONResponse(content={"error": "Invalid amount"}, status_code=400)

    # Create the Stripe session FIRST, then persist 'pending'.
    #
    # This used to UPDATE + commit 'pending' before calling Stripe. When Stripe
    # failed, the row stayed 'pending' with an empty stripe_session_id forever:
    # the board showed money coming in that no checkout session could ever
    # complete, and the deposit link was a dead 500. 24 rows were in that state.
    # The webhook that flips 'pending' -> 'paid' only fires for a session that
    # exists, so a failure here has to leave the lead untouched.
    try:
        checkout = stripe_svc.create_deposit_checkout(
            lead_id=lead["id"],
            customer_name=lead["name"],
            customer_email=lead.get("email") or "",
            project_type=lead.get("project_type") or "",
            amount_cents=amount_cents,
            base_url=f"https://{company()['domain']}",
        )
    except Exception as e:
        logger.warning("[deposit] session creation failed for lead=%s: %s", lead_id, e)
        db.rollback()
        return JSONResponse(
            content={
                "error": str(e),
                "detail": (
                    "Stripe did not create a checkout session, so the deposit was "
                    "not started and this lead was left unchanged. Nothing is owed."
                ),
                "credit_spent": False,
            },
            status_code=502,
        )

    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET deposit_status = 'pending', deposit_cents = %s, "
            "stripe_session_id = %s WHERE id = %s;",
            (amount_cents, checkout["session_id"], lead_id),
        )
        db.commit()
    return JSONResponse(content={
        "url": checkout["url"],
        "amount_cents": amount_cents,
        "session_id": checkout["session_id"],
    })


@app.get("/payments", response_class=HTMLResponse)
async def payments_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute(
            """SELECT p.*, l.name AS lead_name, l.project_type
               FROM payments p LEFT JOIN leads l ON l.id = p.lead_id
               WHERE p.company = 'construction'
               ORDER BY p.created_at DESC LIMIT 500;"""
        )
        payments = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments WHERE company = 'construction';")
        totals = cur.fetchone()
    return templates.TemplateResponse(request=request, name="payments.html", context={
        "user": {"email": user_email},
        "payments": payments,
        "totals": totals,
    })


# --- Stripe -----------------------------------------------------------------
@app.get("/payments/success", response_class=HTMLResponse)
async def payments_success(request: Request, db=Depends(get_db)):
    session_id = request.query_params.get("session_id")
    lead = None
    if session_id:
        with db.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE stripe_session_id = %s;", (session_id,))
            lead = cur.fetchone()
    return templates.TemplateResponse(request=request, name="payment_success.html", context={"lead": lead})


@app.get("/payments/cancel", response_class=HTMLResponse)
async def payments_cancel(request: Request):
    return templates.TemplateResponse(request=request, name="payment_cancel.html", context={})


@app.post("/api/payments/webhook")
async def payments_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = stripe_svc.construct_webhook_event(payload, signature)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook signature verification failed: {e}")

    # construct_event() returns a stripe.Event OBJECT, not a dict. Indexing it
    # like a dict raises KeyError('type') -> HTTP 500 on every payment, so Stripe
    # retries, fails again, and eventually gives up. Read both shapes.
    if isinstance(event, dict):
        etype = event.get("type")
        session = (event.get("data") or {}).get("object") or {}
    else:
        etype = event.type
        session = event.data.object if event.data else {}
    # stripe>=8 objects are NOT mappings: dict(session) raises TypeError. to_dict()
    # exists on StripeObject but not on a plain dict, hence the hasattr guard.
    session = session.to_dict() if hasattr(session, "to_dict") else dict(session)

    if etype == "checkout.session.completed":
        lead_id = session.get("metadata", {}).get("lead_id")
        payment_intent = session.get("payment_intent")
        amount_total = session.get("amount_total", 0)
        currency = session.get("currency", "usd")

        if lead_id:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE leads SET deposit_status = 'paid', stripe_session_id = %s, deposit_cents = %s, "
                    "status = CASE WHEN status IN ('completed','in_progress') THEN status ELSE 'deposit' END "
                    "WHERE id = %s;",
                    (session.get("id"), amount_total, int(lead_id)),
                )
                cur.execute(
                    "INSERT INTO payments (lead_id, stripe_session_id, stripe_payment_intent_id, amount_cents, currency, company) "
                    "VALUES (%s, %s, %s, %s, %s, 'construction') RETURNING id;",
                    (int(lead_id), session.get("id"), payment_intent, amount_total, currency),
                )
                payment_id = cur.fetchone()["id"]
                db.commit()
            # A deposit is cash in, but it is not revenue yet -- it is money
            # held against work not started, so it lands in deposits held and
            # clears to revenue when the job is billed. Booking it as income on
            # the day it arrives overstates what was earned.
            try:
                ledger_sync.ensure_schema(db)
                ledger_sync.sync_deposit(db, payment_id, company=_BID_COMPANY)
            except Exception as e:
                logger.warning("ledger not updated for deposit payment %s: %s", payment_id, e)

    return Response(content='{"received": true}', media_type="application/json")


# --- Business API -----------------------------------------------------------
@app.get("/api/business/summary")
async def business_summary(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'construction';")
        leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM payments WHERE company = 'construction';")
        deposits = cur.fetchone()["c"]
    return {"leads": leads, "deposits": deposits}


# --- SignalWire SMS webhook -------------------------------------------------
@app.post("/comms/sms-webhook")
async def inbound_sms_webhook(request: Request, db=Depends(get_db)):
    form = await request.form()
    From = form.get("From", "")
    Body = form.get("Body", "")

    # STOP / UNSUBSCRIBE: honour it before the bot gets a word in. Every lead
    # with this phone is opted out; the only reply is the confirmation the
    # carriers require.
    if followups.is_opt_out(Body):
        digits = re.sub(r"\D", "", From or "")[-10:]
        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'sms', %s, 'system', %s);", (From, Body))
            if digits:
                cur.execute("SELECT id FROM leads WHERE company = 'construction' AND regexp_replace(COALESCE(phone,''), '\\D', '', 'g') LIKE %s;", (f"%{digits}",))
                ids = [r["id"] for r in cur.fetchall()]
            else:
                ids = []
            db.commit()
        for lid in ids:
            followups.mark_opted_out(db, lid, "sms", Body)
            try:
                zoho_sync.on_opt_out(db, lid)
            except Exception as e:
                print(f"⚠️ zoho opt-out sync failed for {lid}: {e}", flush=True)
        print(f"[sms] opt-out from {From}: {len(ids)} lead(s)", flush=True)
        return Response(content=f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message to="{From}">You're unsubscribed from {brand.short_name()} messages and won't hear from us again. Reply START to re-subscribe.</Message></Response>""", media_type="application/xml")
    agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc))
    ai_reply = await asyncio.to_thread(agent.process_inbound_text, f"Inbound SMS from {From}: {Body}")

    with db.cursor() as cur:
        cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'sms', %s, 'system', %s);", (From, Body))
        db.commit()

    sxml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response><Message to="{From}">{ai_reply}</Message></Response>"""
    return Response(content=sxml_payload, media_type="application/xml")


# --- SignalWire voice webhooks ----------------------------------------------
@app.post("/comms/voice-webhook")
async def voice_webhook(request: Request, db=Depends(get_db)):
    form = await request.form()
    CallSid = form.get("CallSid", "unknown")
    From = form.get("From", "unknown")
    To = form.get("To", "unknown")

    with db.cursor() as cur:
        cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice', %s, %s, %s);", (From, To, f"Voice call received - SID: {CallSid}"))
        db.commit()

    greeting = (
        f"Thank you for calling {company()['name']}! I'm the automated assistant. "
        "I can take down the details of your project and have our estimator call you back "
        "for a free estimate. What are you looking to get done?"
    )
    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna-Neural">{greeting}</Say>
    <Record maxLength="30" playBeep="false" action="/comms/voice-action" transcribe="true" transcribeCallback="/comms/voice-transcribe"/>
</Response>"""
    return Response(content=twiml_payload, media_type="application/xml")


@app.post("/comms/voice-action")
async def voice_action(request: Request, db=Depends(get_db)):
    ai_response = "Thanks for the details. Our team will follow up shortly to schedule your free estimate. Have a great day!"
    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna-Neural">{ai_response}</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml_payload, media_type="application/xml")


@app.post("/comms/voice-transcribe")
async def voice_transcribe(request: Request, db=Depends(get_db)):
    form = await request.form()
    TranscriptionText = form.get("TranscriptionText", "")
    From = form.get("From", "unknown")
    CallSid = form.get("CallSid", "")

    if TranscriptionText:
        try:
            agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc))
            ai_reply = agent.process_inbound_text(f"Voice transcription from {From}: {TranscriptionText}")
        except Exception:
            ai_reply = "Thank you for your call. Our team will follow up shortly."

        with db.cursor() as cur:
            # channel is VARCHAR(10); 'voice-transcription' is 19 chars and raised
            # StringDataRightTruncation, 500-ing this endpoint and aborting the
            # reply insert. Use 'voice', and log the reply as the voice channel
            # it is (it was mislabelled 'sms').
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice', %s, %s, %s);", (From, SIGNALWIRE_NUMBER, TranscriptionText))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'voice', %s, %s, %s);", (SIGNALWIRE_NUMBER, From, ai_reply))
            db.commit()

    return Response(content="", status_code=204)


# --- Outbound AI calls (bot dials leads) -----------------------------------
def _bot_phone_digits(phone):
    if not phone:
        return ""
    return "".join(ch for ch in str(phone) if ch.isdigit())


_CALL_START_HOUR = 9
_CALL_END_HOUR = 19
_CALL_TZ = "America/New_York"


def _follow_up_new_lead(lead_id: int) -> None:
    """AI-call a freshly created lead in a background thread (call only).

    Email follow-up for construction leads is already sent inline by auto_reply at
    each lead-creation site, so this only dials the lead with the outbound voice
    agent — deduped via bot_calls, gated to call hours, and skipped for fake or
    empty phone numbers."""

    def _run():
        try:
            db = psycopg.connect(db_url, row_factory=dict_row)
            db.autocommit = True
        except Exception as e:
            print(f"📞[followup] db failure: {e}", flush=True)
            return
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name, phone, project_type FROM leads WHERE id = %s;",
                    (lead_id,),
                )
                row = cur.fetchone()
            if not row:
                return
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo(_CALL_TZ))
            if not (_CALL_START_HOUR <= now.hour < _CALL_END_HOUR):
                return
            digits = _bot_phone_digits(row.get("phone"))
            if len(digits) < 10:
                return
            with db.cursor() as cur:
                cur.execute("SELECT 1 FROM bot_calls WHERE lead_id = %s LIMIT 1;", (lead_id,))
                if cur.fetchone():
                    return
            to = ("+" + digits) if digits.startswith("1") else ("+1" + digits)
            own = _bot_phone_digits(getattr(signalwire, "from_number", ""))
            if own and digits[-10:] == own[-10:]:
                return
            try:
                # Use the module-level client: it defaults to this app's domain
                # (construction.bizstackperks.com). A bare VapiService() falls
                # back to the hosts domain, which would point the voice brain
                # at the wrong app.
                if not vapi.is_configured():
                    print(f"📞[followup] Vapi not configured, skipping dial for lead {lead_id}", flush=True)
                    return
                sid = vapi.create_ai_outbound_call(to, f"AI immediate callback for new lead {lead_id}")
            except Exception as e:
                print(f"📞[followup] dial failed for lead {lead_id}: {e}", flush=True)
                return
            if sid:
                with db.cursor() as cur:
                    cur.execute(
                        "CREATE TABLE IF NOT EXISTS bot_calls (call_sid TEXT PRIMARY KEY, lead_id INTEGER, "
                        "direction TEXT, status TEXT, turns INTEGER DEFAULT 0, "
                        "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);"
                    )
                    cur.execute(
                        "INSERT INTO bot_calls (call_sid, lead_id, direction, status) "
                        "VALUES (%s, %s, 'outbound', 'dialed') ON CONFLICT (call_sid) DO NOTHING;",
                        (sid, lead_id),
                    )
                    cur.execute(
                        "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                        "VALUES ('outbound', 'voice', 'system', %s, %s);",
                        (to, f"AI immediate callback for new lead {lead_id}"),
                    )
                print(f"📞[followup] dialed lead {lead_id} ({to}) sid={sid}", flush=True)
        except Exception as e:
            print(f"📞[followup] follow-up failed for lead {lead_id}: {e}", flush=True)

    threading.Thread(target=_run, daemon=True).start()


def _bot_lead_for_call(db, phone):
    digits = _bot_phone_digits(phone)
    if len(digits) < 10:
        return None
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, phone, email, project_type, address, budget, timeline, description, status, "
            "estimate_low_cents, estimate_high_cents FROM leads WHERE company = 'construction' "
            "ORDER BY id DESC LIMIT 500;"
        )
        rows = cur.fetchall()
    for r in rows:
        p = _bot_phone_digits(r.get("phone"))
        if p and p[-10:] == digits[-10:]:
            return r
    return None


def _bot_lead_context(lead):
    if not lead:
        return "No existing lead record for this number."
    ctx = f"Lead: {lead.get('name') or 'Unknown'} (id {lead['id']}), project: {lead.get('project_type') or 'home project'}"
    if lead.get("address"):
        ctx += f", address: {lead['address']}"
    if lead.get("budget"):
        ctx += f", budget: {lead['budget']}"
    if lead.get("timeline"):
        ctx += f", timeline: {lead['timeline']}"
    if lead.get("description"):
        ctx += f", notes: {lead['description'][:200]}"
    if lead.get("estimate_low_cents") and lead.get("estimate_high_cents"):
        ctx += f", already quoted ballpark: ${lead['estimate_low_cents'] // 100:,}-${lead['estimate_high_cents'] // 100:,}"
    ctx += f", status: {lead.get('status') or 'new'}. This is an outgoing call: be warm and brief, answer questions, and drive toward a deposit or a free walkthrough."
    return ctx


def _outbound_turn(db, sid, to, text, turns):
    max_turns = int(os.getenv("OUTBOUND_MAX_TURNS", "6"))
    lead = _bot_lead_for_call(db, to)
    context = _bot_lead_context(lead)
    if text:
        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice', %s, %s, %s);", (sid, SIGNALWIRE_NUMBER, text))
            db.commit()
        try:
            agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc))
            ai_reply = agent.process_inbound_text(f"Outbound AI call to a lead. Context: {context}\nLead's response: {text}")
        except Exception:
            ai_reply = "Thanks! Our estimator will follow up shortly to schedule your free walkthrough."
        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'voice', 'system', %s, %s);", (sid, ai_reply))
            db.commit()
    else:
        ai_reply = "Hi! I didn't quite catch that. If you're there, tell me what project you'd like done, or just say later anytime."

    turns = min(int(turns or 0) + 1, max_turns)
    with db.cursor() as cur:
        cur.execute("UPDATE bot_calls SET status = 'in_progress', turns = %s WHERE call_sid = %s;", (turns, sid))
        db.commit()

    if turns >= max_turns:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna-Neural">{ai_reply}</Say>
    <Say voice="Polly.Joanna-Neural">Thanks so much. We will follow up shortly. Have a great day!</Say>
    <Hangup/>
</Response>"""
    else:
        twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna-Neural">{ai_reply}</Say>
    <Record maxLength="60" playBeep="false" action="/comms/outbound-voice-action" transcribe="true" transcribeCallback="/comms/outbound-voice-transcribe"/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/comms/outbound-voice-webhook")
async def outbound_voice_webhook(request: Request, db=Depends(get_db)):
    form = await request.form()
    sid = form.get("CallSid", "")
    to = form.get("To", "unknown")
    lead = _bot_lead_for_call(db, to)
    with db.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS bot_calls (call_sid TEXT PRIMARY KEY, lead_id INTEGER, "
            "direction TEXT, status TEXT, turns INTEGER DEFAULT 0, "
            "created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP);"
        )
        try:
            cur.execute("ALTER TABLE bot_calls ALTER COLUMN status TYPE TEXT;")
        except Exception:
            pass
        cur.execute(
            "INSERT INTO bot_calls (call_sid, lead_id, direction, status) VALUES (%s, %s, 'outbound', 'answering') "
            "ON CONFLICT (call_sid) DO NOTHING;",
            (sid, lead["id"] if lead else None),
        )
        cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'voice', 'system', %s, %s);", (to, f"Outbound AI call placed - SID: {sid}"))
        db.commit()

    if lead and lead.get("name"):
        first = auto_reply._person_first(lead["name"])
        greeting = (
            f"Hi {first}, this is the assistant from {company()['name']} calling about your "
            f"{lead.get('project_type') or 'project'} request. Quick one — are you still interested in a free estimate?"
        ) if first else (
            f"Hi there, this is the assistant from {company()['name']}. "
            f"I'm calling about your recent {lead.get('project_type') or 'public sector'} project request. Are you still interested in a free estimate?"
        )
    else:
        greeting = (
            f"Hi there, this is the assistant from {company()['name']}. "
            "I'm calling about your recent home project request. Are you still interested in a free estimate?"
        )
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="Polly.Joanna-Neural">{greeting}</Say>
    <Record maxLength="60" playBeep="false" action="/comms/outbound-voice-action" transcribe="true" transcribeCallback="/comms/outbound-voice-transcribe"/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/comms/outbound-voice-action")
async def outbound_voice_action(request: Request, db=Depends(get_db)):
    form = await request.form()
    sid = form.get("CallSid", "")
    to = form.get("To", "")
    text = form.get("TranscriptionText", "")
    turns = "0"
    with db.cursor() as cur:
        cur.execute("SELECT turns FROM bot_calls WHERE call_sid = %s;", (sid,))
        row = cur.fetchone()
        if row:
            turns = row["turns"]
    return _outbound_turn(db, sid, to, text, turns)


@app.post("/comms/outbound-voice-transcribe")
async def outbound_voice_transcribe(request: Request, db=Depends(get_db)):
    form = await request.form()
    sid = form.get("CallSid", "")
    to = form.get("To", "")
    text = form.get("TranscriptionText", "")
    turns = "0"
    with db.cursor() as cur:
        cur.execute("SELECT turns FROM bot_calls WHERE call_sid = %s;", (sid,))
        row = cur.fetchone()
        if row:
            turns = row["turns"]
    return _outbound_turn(db, sid, to, text, turns)


# --- SWML live voice agent (SignalWire AI conversation) ---------------------
VOICE_AGENT_PROMPT = f"""\
You are the {company()['name']} voice assistant, answering calls 24/7.
Speak naturally, warmly, and concisely: short sentences, conversational, human-sounding.

ABOUT THE COMPANY
- {company()['name']} is a licensed, insured general contractor serving {company()['service_area']}.
- We do whole-home renovations and repairs: kitchens, baths, drywall, roofing, plumbing, electrical, carpentry, tile, flooring, decks, and fences.
- Website: https://{company()['domain']}. Phone, call or text 24/7: {company()['phone']}. Email: {company()['email']}.
- We are the sister company to Broom Service, so we also turn short-term rentals (Airbnb/Vrbo) into stay-ready, revenue-producing properties.

HOW TO QUOTE CONSTRUCTION WORK (real numbers, right on the call):
1. ALWAYS get the property street address first and run lookup_property to pull the real square footage, beds, baths, and year built. Ground the quote in the actual home, never a guess.
2. Ask what they want done in plain words (kitchen, bath, whole-home, roof, etc.) and WHAT MATERIAL they have in mind. Ask directly: for a roof ask "3-tab, architectural, or architectural 30- or 40-year?" and "any metal?"; for counters ask "laminate, quartz, granite, or marble?"; for framing/deck ask "standard, premium, or engineered lumber?"; for siding, flooring, tile, windows, cabinets ask the style/brand they're considering. The caller may not know — offer the standard/entry choice as the default and note what upgrading does to the price.
3. Call quote_project with project_type + address (+ materials text combining their answers) to get a ballpark range computed from the real property and the grades they chose. Speak the range plainly, e.g. "for an architectural 30-year shingle roof on a ~1,800 sq ft home, ballpark is about $19,000 to $33,000 — final price comes from the free on-site walkthrough."
4. If lookup_property finds the address but comes back with NO square footage (the property records may only give address/geo data), ask the caller for the approximate square footage — or the general home size ("small, medium, large") if they don't know — and pass that into quote_project via the sqft field. Only when the caller truly can't give any size at all should you rely on the assumed-size estimate, and still tell them it's based on an assumed home size.
5. quote_project saves the lead stamped with the range if you provide the caller's name and phone. Confirm their name and best number, pass them through, and offer the free on-site walkthrough for the exact written price.
6. If the caller wants to move forward on the spot, create the deposit payment link with create_deposit_link (after registering the lead) and text it to them mid-call with send_sms_message so they can lock in the project and the slot today.
- The range from quote_project is a ballpark to qualify, never a firm bid — the written fixed price always comes from the free on-site walkthrough.
- We give a clear written scope, a fixed price (not open-ended time and materials), and a schedule.

GENERAL
- Use find_people when you want to recognize who's calling (an employee, a returning customer) or pull up someone's record.
- Use navigation_guide to walk an employee, customer, or the owner through our websites/apps (quotes, crew app, dashboards).
- Point callers to https://{company()['domain']} or the free-estimate form.
- Never expose internal data, credentials, or secrets. Never invent prices or schedules beyond quote_project's range.
- If a caller is distressed or reports an emergency (gas leak, flooding, no power), tell them to call 911 or the appropriate emergency service first.
"""

VOICE_TOOL_URL = (os.getenv("APP_BASE_URL", "") or "") + "/api/voice/tool"

_VOICE_KNOWLEDGE_FILES = [
    Path(__file__).resolve().parent / "bot_knowledge.md",
    Path(__file__).resolve().parent / "construction_knowledge.md",
]


def _voice_prompt() -> str:
    """Combine the live knowledge base files with the construction voice prompt at request time."""
    parts = [VOICE_AGENT_PROMPT]
    for path in _VOICE_KNOWLEDGE_FILES:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        parts.append("## OPERATING MANUAL — " + path.name + "\n" + text)
    return "\n\n".join(parts)


def _swaig_parameters(props: dict, required: list, notes: str = ""):
    return {
        "type": "object",
        "required": required,
        "properties": props,
        "$comment": notes,
        "additionalProperties": False,
    }


def _build_voice_swml(prompt: str, ai_params_extra: dict | None = None) -> dict:
    ai_params = {"ai_model": "gpt-4.1", "temperature": 0.7, "frequency_penalty": 0.3}
    if ai_params_extra:
        ai_params.update(ai_params_extra)
    return {
        "version": "1.0.0",
        "sections": {
            "main": [
                {"answer": {}},
                {
                    "ai": {
                        "prompt": {"text": prompt},
                        "languages": [
                            {
                                "name": "English",
                                "code": "en-US",
                                "voice": "elevenlabs.rachel",
                                "speech_fillers": ["one moment please,", "hmm...", "let's see,"],
                            }
                        ],
                        "params": ai_params,
                        "post_prompt_url": (os.getenv("APP_BASE_URL", "") or "") + "/api/voice/debug",
                        "pronounce": [
                            {"replace": "Buildstack", "with": "build stack", "ignore_case": True},
                            {"replace": "Vrbo", "with": "virbo", "ignore_case": True},
                            {"replace": "Airbnb", "with": "air bnb", "ignore_case": True},
                        ],
                        "SWAIG": {
                            "defaults": {"web_hook_url": VOICE_TOOL_URL},
                            "functions": [
                                {
                                    "function": "register_lead",
                                    "description": "Save a new project request / lead with the caller's details. Use as soon as you have a name and phone number.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "name": {"type": "string", "description": "Caller's full name."},
                                            "phone": {"type": "string", "description": "Caller's phone in E.164, e.g. +17578469275."},
                                            "email": {"type": "string", "description": "Optional email."},
                                            "project_type": {"type": "string", "description": "Type of work, e.g. Kitchen, Whole-Home Renovation, Roofing."},
                                            "address": {"type": "string", "description": "Property address for the estimate."},
                                            "budget": {"type": "string", "description": "Any budget mentioned."},
                                            "timeline": {"type": "string", "description": "Any timeline mentioned."},
                                            "notes": {"type": "string", "description": "Other project details."},
                                        },
                                        ["name", "phone"],
                                    ),
                                },
                                {
                                    "function": "lookup_leads",
                                    "description": "Look up a caller's prior requests by phone number.",
                                    "parameters": _swaig_parameters(
                                        {"phone": {"type": "string", "description": "Phone used on the request."}},
                                        ["phone"],
                                    ),
                                },
                                {
                                    "function": "send_sms_message",
                                    "description": "Send a text message to a phone number (e.g. a link to the site or a scheduling note).",
                                    "parameters": _swaig_parameters(
                                        {
                                            "to": {"type": "string", "description": "Destination phone in E.164."},
                                            "body": {"type": "string", "description": "Text content."},
                                        },
                                        ["to", "body"],
                                    ),
                                },
                                {
                                    "function": "send_email_message",
                                    "description": "Send an email to a client (e.g. written scope, receipts, follow-ups).",
                                    "parameters": _swaig_parameters(
                                        {
                                            "to": {"type": "string", "description": "Recipient email."},
                                            "subject": {"type": "string", "description": "Email subject."},
                                            "body": {"type": "string", "description": "Email body text."},
                                        },
                                        ["to", "subject", "body"],
                                    ),
                                },
                                {
                                    "function": "estimate_materials",
                                    "description": "Build a line-item material cost estimate for a project type (kitchen, bath, whole-home, roofing, drywall, deck/fence, handyman, masonry, plumbing, electrical). Never quote materials from memory.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "project_type": {"type": "string", "description": "Project type: whole-home, kitchen, bath, roofing, drywall, deck/fence, handyman, masonry, plumbing, electrical, etc."},
                                            "sqft": {"type": "number", "description": "Approximate square footage the work covers."},
                                            "live": {"type": "boolean", "description": "Try a live materials API if configured; else price book."},
                                        },
                                        ["project_type"],
                                        "Convert the caller's scope to sqft first when possible.",
                                    ),
                                },
                                {
                                    "function": "get_material_price",
                                    "description": "Look up the current price for a single material SKU (e.g. stud_2x4x8, drywall_sheet_1/2, shingles_per_square, pex_a_1/2_per_ft, copper_wire_per_lb).",
                                    "parameters": _swaig_parameters(
                                        {"sku": {"type": "string", "description": "Material SKU to look up."}},
                                        ["sku"],
                                    ),
                                },
                                {
                                    "function": "search_materials",
                                    "description": "Search the Buildstack materials catalog for specific products by brand, style, color, category, or text — returns real brands with model numbers and store SKU/item numbers (Home Depot, Lowe's) plus 2026 retail ballpark prices. Use when a caller asks for a specific brand/style (e.g. 'Hampton Bay shaker cabinets', 'GAF Timberline HDZ shingles charcoal', 'LVP flooring', 'MOEN kitchen faucet') so you can name exact products + item numbers instead of guessing. Never invent a brand or SKU — only return what this tool returns.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "category": {"type": "string", "description": "Optional category: cabinets, countertops, flooring, roofing, fixtures."},
                                            "brand": {"type": "string", "description": "Optional brand, e.g. Hampton Bay, GAF, MOEN, Delta, TrafficMaster, Shaw."},
                                            "query": {"type": "string", "description": "Optional free-text search, e.g. 'shaker', 'charcoal', 'kitchen faucet', 'LVP'."},
                                            "store": {"type": "string", "description": "Optional store: Home Depot or Lowe's."},
                                            "limit": {"type": "integer", "description": "Max results to return (default 8)."},
                                        },
                                        [],
                                        "Search the catalog when the caller names a brand, style, or asks what a material costs.",
                                    ),
                                },
                                {
                                    "function": "create_deposit_link",
                                    "description": "Create a Stripe deposit-checkout link for a saved lead so they can reserve the project. Use AFTER register_lead returns the lead id, then text the link with send_sms_message.",
                                    "parameters": _swaig_parameters(
                                        {"lead_id": {"type": "integer", "description": "The lead id returned by register_lead."}},
                                        ["lead_id"],
                                    ),
                                },
                                {
                                    "function": "list_leads",
                                    "description": "Look up clients/leads (statuses like new/contacted/quoted/deposit/in_progress/completed/lost). Use when the owner or a client asks about a project or lead status.",
                                    "parameters": _swaig_parameters(
                                        {"status": {"type": "string", "description": "Optional status filter: new, contacted, quoted, deposit, in_progress, completed, lost."}},
                                        [],
                                    ),
                                },
                                {
                                    "function": "update_lead_status",
                                    "description": "Move a lead to a new status (new, contacted, quoted, deposit, in_progress, completed, lost).",
                                    "parameters": _swaig_parameters(
                                        {
                                            "lead_id": {"type": "integer", "description": "Lead id to update."},
                                            "status": {"type": "string", "description": "New status: new, contacted, quoted, deposit, in_progress, completed, or lost."},
                                        },
                                        ["lead_id", "status"],
                                    ),
                                },
                                {
                                    "function": "lookup_permits",
                                    "description": "Look up local permits/permits we found by city or address.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "city": {"type": "string", "description": "City to search, e.g. Norfolk."},
                                            "address": {"type": "string", "description": "Street address to search."},
                                        },
                                        [],
                                    ),
                                },
                                {
                                    "function": "list_crew",
                                    "description": "List the crew with roles and contact info. Use for owner or scheduling questions.",
                                    "parameters": _swaig_parameters({}, []),
                                },
                                {
                                    "function": "lookup_crew_timesheets",
                                    "description": "Look up submitted crew timesheets, optionally filtered by crew member, project, or status (submitted/approved).",
                                    "parameters": _swaig_parameters(
                                        {
                                            "crew_id": {"type": "integer", "description": "Optional crew member id."},
                                            "project_id": {"type": "integer", "description": "Optional project id."},
                                            "status": {"type": "string", "description": "Optional timesheet status."},
                                        },
                                        [],
                                    ),
                                },
                                {
                                    "function": "get_accounting_summary",
                                    "description": "Pull revenue (payments + deposits) and payroll totals. Use for owner accounting questions.",
                                    "parameters": _swaig_parameters({}, []),
                                },
                                {
                                    "function": "get_business_summary",
                                    "description": "Quick totals: open leads, deposits collected, payroll status. Use for a fast business pulse check.",
                                    "parameters": _swaig_parameters({}, []),
                                },
                                {
                                    "function": "make_outbound_call",
                                    "description": "Place an outgoing phone call to a number RIGHT NOW and answer it as the voice agent (AI). Use when you promised to call someone back, or the owner asks the bot to reach out by phone.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "to": {"type": "string", "description": "Destination phone number in E.164 format, e.g. +17558469275."},
                                            "notes": {"type": "string", "description": "Optional reminder of why the call is being placed."},
                                        },
                                        ["to"],
                                    ),
                                },
                                {
                                    "function": "lookup_property",
                                    "description": "Look up a US property by street address and return square footage, beds, baths, year built, and stories. Use for any construction quote to size the job instead of guessing. NOTE: the property record may return only address/geo data with NO sqft/beds/baths — if sqft comes back empty, ask the caller for approximate square footage before quoting.",
                                    "parameters": _swaig_parameters(
                                        {"address": {"type": "string", "description": "Full property street address, e.g. 456 Oak Ave, Virginia Beach VA."}},
                                        ["address"],
                                    ),
                                },
                                {
                                    "function": "quote_project",
                                    "description": "Give a REAL ballpark quote for any construction trade (kitchen, bath, whole-home, roof, drywall, deck/fence, plus all trades). Looks up the property's square footage from the address, applies material grades the caller names (shingles 3-tab/architectural/20/30/40yr, countertop laminate/quartz/granite/marble, lumber standard/premium/engineered), saves the lead stamped with the range, and returns a spoken quote. If no sqft is available (address lookup had no size and caller gave none), it falls back to an assumed ~1,750 sq ft home and marks needs_sqft — say you can tighten the range if they give the square footage.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "project_type": {"type": "string", "description": "Type of work: kitchen, bath, whole-home, roof, drywall, deck/fence, basement, electrical, plumbing, etc."},
                                            "address": {"type": "string", "description": "Optional property street address. Use it so we can look up sqft."},
                                            "sqft": {"type": "number", "description": "Approximate square footage if known. Ask the caller for it when the address lookup doesn't return a size."},
                                            "materials": {"type": "string", "description": "Material choices the caller names, free text, e.g. 'architectural 30-year shingles, marble countertops, premium lumber'."},
                                            "name": {"type": "string", "description": "Optional caller name to attach."},
                                            "phone": {"type": "string", "description": "Optional caller phone in E.164."},
                                            "notes": {"type": "string", "description": "Optional project details."},
                                            "lead_id": {"type": "integer", "description": "Optional lead id to stamp the quoted range onto."},
                                        },
                                        ["project_type"],
                                        "Get the property address and pull real sqft; if no size comes back, ask the caller for approximate square footage before quoting; otherwise quote on the assumed size and note it.",
                                    ),
                                },
                                {
                                    "function": "get_inventory",
                                    "description": "List construction supply inventory on hand, optionally by category or only items at/below reorder level. Use for 'what supplies do we have' / 'what do we need' questions.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "company": {"type": "string", "description": "construction."},
                                            "category": {"type": "string", "description": "Optional category filter, e.g. roofing, lumber."},
                                            "low_only": {"type": "boolean", "description": "Only show items at or below reorder level."},
                                        },
                                        [],
                                    ),
                                },
                                {
                                    "function": "update_inventory",
                                    "description": "Adjust construction supply quantity on hand. Pass qty_delta to add/remove, or set_qty to set an absolute value. Use when supplies are used, restocked, or purchased.",
                                    "parameters": _swaig_parameters(
                                        {
                                            "item_name": {"type": "string", "description": "Exact inventory item name."},
                                            "qty_delta": {"type": "number", "description": "Signed change: +5 for restock, -2 for used."},
                                            "company": {"type": "string", "description": "construction."},
                                            "category": {"type": "string", "description": "Optional category when creating the item."},
                                            "set_qty": {"type": "number", "description": "Optional absolute quantity to set."},
                                        },
                                        ["item_name"],
                                    ),
                                },
                                {
                                    "function": "reorder_report",
                                    "description": "List every construction supply at or below its reorder level — the restock shopping list.",
                                    "parameters": _swaig_parameters(
                                        {"company": {"type": "string", "description": "construction."}},
                                        [],
                                    ),
                                },
                                {
                                    "function": "find_people",
                                    "description": "Search employees (crew), customers, and leads by name, phone, or email. Use to recognize who's on the phone or to pull up a person's record.",
                                    "parameters": _swaig_parameters(
                                        {"query": {"type": "string", "description": "Name, phone, or email to search."}},
                                        ["query"],
                                    ),
                                },
                                {
                                    "function": "navigation_guide",
                                    "description": "Explain how to navigate our websites/apps for employees (crew), customers, or the owner (crew app, quotes, dashboards).",
                                    "parameters": _swaig_parameters(
                                        {
                                            "role": {"type": "string", "description": "employee, customer, or owner."},
                                            "company": {"type": "string", "description": "Optional: construction."},
                                        },
                                        [],
                                    ),
                                },
                            ],
                        },
                    }
                },
            ]
        },
    }


@app.api_route("/voice.swml", methods=["GET", "POST"])
@app.api_route("/voice-app.swml", methods=["GET", "POST"])
async def voice_swml():
    return JSONResponse(content=_build_voice_swml(_voice_prompt()))


@app.api_route("/comms/outbound-voice.swml", methods=["GET", "POST"])
async def outbound_voice_swml(request: Request, db=Depends(get_db)):
    to = request.query_params.get("to") or request.query_params.get("To") or ""
    if not to:
        try:
            form = dict(await request.form())
            to = form.get("to") or form.get("To") or ""
        except Exception:
            pass
    digits = "".join(ch for ch in str(to or "") if ch.isdigit())
    greeting = ""
    if len(digits) >= 10:
        try:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT id, name, phone, email, project_type, address, budget, timeline, description, status "
                    "FROM leads WHERE phone IS NOT NULL ORDER BY id DESC LIMIT 300;"
                )
                for r in cur.fetchall():
                    p = "".join(ch for ch in str(r.get("phone") or "") if ch.isdigit())
                    if p and p[-10:] == digits[-10:]:
                        greeting = (
                            f"OUTBOUND CALL — you are the {company()['name']} voice assistant. "
                            f"You are calling {r.get('name') or 'this lead'}"
                            + (f" ({r.get('email') or r.get('phone')})" if (r.get('email') or r.get('phone')) else "")
                            + (f" about a {r.get('project_type')} project" if r.get("project_type") else "")
                            + (f" at {r.get('address')}" if r.get("address") else "")
                            + f" (lead #{r['id']}). Greet them by first name, briefly remind them why you are calling, "
                              f"and keep it quick, friendly, and low-pressure. Work the conversation from there — it is a live human."
                        )
                        break
        except Exception as e:
            print(f"⚠️ OUTBOUND-SWML lead lookup failed: {e}", flush=True)
    prompt = (greeting + "\n\n" if greeting else "") + _voice_prompt()
    return JSONResponse(content=_build_voice_swml(
        prompt,
        ai_params_extra={
            "direction": "outbound",
            "wait_for_user": True,
            "outbound_attention_timeout": 20000,
        },
    ))


def _voice_openai_tools() -> list:
    """Derive OpenAI function schemas from the canonical SWAIG function list."""
    try:
        sw = _build_voice_swml(_voice_prompt())
        funcs = sw["sections"]["main"][1]["ai"]["SWAIG"].get("functions", [])
    except Exception:
        funcs = []
    out = []
    for f in funcs:
        name = f.get("function")
        if not name or name not in VOICE_ALLOWED_TOOLS:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": f.get("description", ""),
                "parameters": f.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return out


def _voice_tool_dispatch(db, name: str, args: dict) -> str:
    """Run one voice tool and turn its result into spoken text."""
    if name not in VOICE_ALLOWED_TOOLS:
        return "I'm not able to do that yet, but the team will follow up by text."
    if not isinstance(args, dict):
        return "I didn't catch all the details. Could you repeat the date and time?"
    try:
        handlers = build_tool_handlers(db, stripe_svc)
        result = handlers[name](**args)
    except TypeError as e:
        print(f"⚠️ VAPI-TOOL bad args: {e}", flush=True)
        return "I didn't catch all the details. Could you repeat the date and time?"
    except Exception as e:
        print(f"⚠️ VAPI-TOOL error: {e}", flush=True)
        return "That hit a snag — I'll have the team follow up by text."
    return _swaig_tool_response_text(name, result)


def _vapi_messages_to_openai(payload: dict) -> list:
    """Convert Vapi customLLM messages into OpenAI-format messages with our voice prompt.

    Vapi always injects its own generic system message ("You are an assistant."),
    which would otherwise overwrite our voice brain — so inbound system messages
    are dropped and our `_voice_prompt()` stays authoritative.
    """
    conversation = [{"role": "system", "content": _voice_prompt()}]
    for m in payload.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "system":
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts = []
            for seg in content:
                if isinstance(seg, dict):
                    parts.append(str(seg.get("text") or seg.get("value") or ""))
                elif isinstance(seg, str):
                    parts.append(seg)
            content = " ".join(p for p in parts if p)
        elif content is None:
            continue
        if role not in ("user", "assistant", "tool"):
            continue
        item = {"role": role, "content": str(content)}
        if role == "tool":
            item["tool_call_id"] = m.get("toolCallId") or m.get("tool_call_id") or "vapi_call"
        conversation.append(item)
    return conversation


_CATALOG_CATEGORY_KEYWORDS = {
    "cabinets": ("cabinet", "cabinets", "cabinetry"),
    "countertops": ("counter", "counters", "countertop", "countertops"),
    "flooring": ("flooring", "floor", "floors", "hardwood", "vinyl plank", "lvp", "carpet"),
    "fixtures": ("fixture", "fixtures", "faucet", "faucets", "shower", "toilet", "sink"),
    "roofing": ("roof", "roofing", "shingle", "shingles"),
}

_CATALOG_PLAN_CONTEXT_TURNS = 4


def _vapi_catalog_search_plan(messages):
    """Work out which material categories this turn is really asking about.

    The model tends to answer brand/price questions straight from memory instead
    of calling search_materials, which is how it starts quoting item numbers that
    aren't in the catalog. Scanning the recent conversation for actual category
    words gives the caller something concrete to force a real catalog lookup on.
    System turns are skipped on purpose: the voice prompt always lists the whole
    catalog, so it would match every single question.
    """
    if not messages:
        return None
    recent = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
    recent = recent[-_CATALOG_PLAN_CONTEXT_TURNS:]
    found = []
    for message in recent:
        content = str(message.get("content") or "").lower()
        if not content:
            continue
        for category, words in _CATALOG_CATEGORY_KEYWORDS.items():
            if category in found:
                continue
            if any(word in content for word in words):
                found.append(category)
    return found or None


def _vapi_assistant_text(client, model, messages, tools, db,
                         tool_choice=None, material_categories=None) -> str:
    """Run the voice conversation through OpenAI, executing allowed tools, and
    return the final assistant text. Mirrors the SWML tool loop.

    tool_choice forces one tool call on the opening turn (used to pin
    search_materials), and material_categories rewrites that first lookup so the
    model's own brand/store picks can't filter the catalog down to nothing.
    """
    material_categories = list(material_categories or [])
    material_categories = [c for c in material_categories if c]
    # A caller on a phone will not wait forever. Each turn is a full model
    # round-trip against a 40k-character prompt, and a model that keeps asking
    # for tools can burn the whole budget before saying a word. When the
    # budget runs out we answer with whatever we have, so the caller hears
    # something instead of silence.
    try:
        deadline_s = float(os.getenv("VOICE_TOOL_DEADLINE_SECONDS", "25") or 25)
    except (TypeError, ValueError):
        deadline_s = 25.0
    started = time.monotonic()
    last_text = ""
    for turn in range(6):
        if time.monotonic() - started > deadline_s:
            print(f"⚠️ VAPI-DEADLINE hit after {time.monotonic() - started:.1f}s", flush=True)
            break
        call_kwargs = {"model": model, "messages": messages, "tools": tools, "temperature": 0.7}
        if turn == 0 and tool_choice:
            call_kwargs["tool_choice"] = tool_choice
        resp = client.chat.completions.create(**call_kwargs)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return (msg.content or "").strip() or "One moment — let me check that for you."
        if (msg.content or "").strip():
            last_text = msg.content.strip()
        calls = []
        for tc in tool_calls:
            calls.append({"id": tc.id, "type": "function",
                          "function": {"name": tc.function.name, "arguments": tc.function.arguments}})
        messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": calls})
        for tc in tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if turn == 0 and material_categories and tc.function.name == "search_materials":
                args.pop("brand", None)
                args.pop("store", None)
                args["category"] = " ".join(material_categories)
                args["query"] = ""
                args["limit"] = 9
            try:
                result = _voice_tool_dispatch(db, tc.function.name, args)
            except Exception as e:
                print(f"⚠️ VAPI-TOOL {tc.function.name} crash: {e}", flush=True)
                result = "That hit a snag — the team will follow up by text."
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": str(result)})
    return last_text or "Let me pass this to the team to get you squared away — they'll follow up by text."


def _vapi_sse_response(text: str, model: str, completion_id: str) -> StreamingResponse:
    """Stream assistant text back to Vapi as OpenAI-format SSE chunks."""
    def gen():
        created = int(time.time())
        words = (text + " ").split(" ")
        for i, word in enumerate(words):
            # OpenAI opens a stream with a role-only chunk. Vapi tolerates its
            # absence, but sending it keeps the shape canonical.
            delta = {"role": "assistant", "content": word + " "} if i == 0 else {"content": word + " "}
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [{"index": 0, "delta": delta,
                             "logprobs": None, "finish_reason": None}],
            }
            yield f"data: {json.dumps(chunk)}\n\n"
        done = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "logprobs": None, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(done)}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


VOICE_MODEL_DOWN_TEXT = (
    "I'm sorry, I'm having trouble on my end right now. Please try calling back "
    "in a few minutes, or request a free quote on our website and we'll call you."
)


@app.api_route("/vapi/llm", methods=["POST"])
@app.api_route("/vapi/llm/chat/completions", methods=["POST"])
async def vapi_llm(request: Request, db=Depends(get_db)):
    """OpenAI-compatible chat completion served to Vapi's customLLM provider.

    Runs the same voice prompt + tool harness as the SWML agent, so the brains
    stay entirely in this app. Vapi only supplies the transcript + audio.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    from openai import OpenAI
    # Same treatment as the hosts app: voice gets its own base URL rather than
    # sharing OPENAI_BASE_URL, because repointing that would drag web_search
    # and every other OpenAI caller with it -- and search uses the Responses
    # API, which is not Zen-compatible.
    _base = (os.getenv("VOICE_BASE_URL") or "").strip()
    _key = os.getenv("OPENAI_API_KEY") or ""
    if _base:
        # Zen's free models need no credential. The SDK refuses to construct
        # with an empty api_key, so pass a placeholder and blank the header;
        # a non-empty Authorization gets an instant 401.
        _noauth = os.getenv("VOICE_NO_AUTH", "").lower() in ("1", "true", "yes")
        client = OpenAI(api_key=_key or "zen",
                        base_url=_base.rstrip("/"),
                        default_headers={"Authorization": ""} if _noauth else None)
    else:
        client = OpenAI(api_key=_key)
    model = os.getenv("OPENAI_VOICE_MODEL", "gpt-4o-mini")
    messages = _vapi_messages_to_openai(payload)
    tools = _voice_openai_tools() or None

    material_categories = _vapi_catalog_search_plan(messages)
    forced_choice = None
    if material_categories:
        forced_choice = {"type": "function", "function": {"name": "search_materials"}}
    try:
        text = _vapi_assistant_text(client, model, messages, tools, db,
                                    tool_choice=forced_choice,
                                    material_categories=material_categories)
    except Exception as e:
        # A 500 here is the worst outcome on a phone call: Vapi speaks the
        # greeting and then goes silent for the rest of the call. That is what
        # happened on 2026-10-28 when the OpenAI key was rejected (401) -- every
        # caller to the business line got dead air. Speak a fallback instead,
        # and log loudly: a model failure on the voice line is an outage.
        print(f"❌ VAPI-LLM model call failed ({type(e).__name__}): {str(e)[:300]}", flush=True)
        text = VOICE_MODEL_DOWN_TEXT
    completion_id = "chatcmpl-" + str(uuid.uuid4()).replace("-", "")
    if payload.get("stream"):
        return _vapi_sse_response(text, model, completion_id)
    created = int(time.time())
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0,
                     "message": {"role": "assistant", "content": text},
                     "logprobs": None,
                     "finish_reason": "stop"}],
    }


VOICE_ALLOWED_TOOLS = {
    "register_lead",
    "lookup_leads",
    "send_sms_message",
    "send_email_message",
    "estimate_materials",
    "get_material_price",
    "search_materials",
    "create_deposit_link",
    "list_leads",
    "update_lead_status",
    "lookup_permits",
    "list_crew",
    "lookup_crew_timesheets",
    "get_accounting_summary",
    "get_business_summary",
    "make_outbound_call",
    "lookup_property",
    "quote_project",
    "get_inventory",
    "update_inventory",
    "reorder_report",
    "find_people",
    "navigation_guide",
}


def _swaig_tool_response_text(name: str, result) -> str:
    if isinstance(result, dict) and result.get("ok") is False:
        return str(result.get("error") or result.get("message") or "That didn't work — the team will follow up.")
    if name == "register_lead":
        return "Got it — I saved your project details and our estimator will call you back to schedule a free estimate."
    if name == "lookup_leads":
        leads = result.get("leads") or []
        if not leads:
            return f"No prior request found for {result.get('phone', 'that number')}."
        lines = [f"{l.get('project_type') or 'Project'} — {l.get('status')}" for l in leads[:3]]
        return "Requests: " + "; ".join(lines)
    if name == "send_sms_message":
        return "Text sent." if result.get("ok") else str(result.get("error") or "Text couldn't be sent.")
    if name == "send_email_message":
        if result.get("ok"):
            return f"Email sent to {result.get('sent_to')}."
        return str(result.get("error") or "Email couldn't be sent.")
    if name == "estimate_materials":
        est = (result.get("estimate") or {}) if result.get("ok") else {}
        lines = est.get("lines") or []
        if result.get("ok") and lines:
            total_low = est.get("total_low_dollars") or 0
            total_high = est.get("total_high_dollars") or 0
            return f"Materials ballpark: ${total_low:,.0f} to ${total_high:,.0f}. Source: {est.get('source', 'price book')}."
        return str(result.get("error") or "I can't pull material pricing on that right now.")
    if name == "get_material_price":
        if result.get("ok"):
            return f"{result.get('sku')} is about ${result.get('price_dollars'):,.2f}."
        return str(result.get("error") or "I couldn't find that material.")
    if name == "search_materials":
        items = result.get("items") or []
        if not items:
            return "I don't have that exact product in the catalog yet, but the builder gives exact brands and item numbers on the free in-person quote."

        def _describe_material(it):
            brand = it.get("brand") or ""
            model = it.get("model") or ""
            color = it.get("color") or ""
            sku = it.get("sku") or ""
            how = f" (item {sku})" if sku else f" (model {model})" if model else ""
            if it.get("unit") == "sqft":
                if it.get("price_high_dollars"):
                    return f"{brand} {it.get('name', '')} {color} at roughly ${it.get('price_dollars'):.0f} to ${it.get('price_high_dollars'):.0f} per square foot{how}"
                return f"{brand} {it.get('name', '')} {color} at about ${it.get('price_dollars'):.2f} per square foot{how}"
            if it.get("price_high_dollars"):
                return f"{brand} {it.get('name', '')} {color} roughly ${it.get('price_dollars'):,.0f} to ${it.get('price_high_dollars'):,.0f} each{how}"
            return f"{brand} {it.get('name', '')} {color} about ${it.get('price_dollars'):,.2f} each{how}"

        def _category_of(it):
            return str(it.get("category") or "").strip().lower()

        categories = []
        for it in items:
            cat = _category_of(it)
            if cat and cat not in categories:
                categories.append(cat)

        if len(categories) > 1:
            spoken = []
            for cat in categories:
                first = next(it for it in items if _category_of(it) == cat)
                spoken.append(f"{cat.title()} option, {_describe_material(first)}")
            retailers = []
            for it in items:
                for part in str(it.get("store") or "").split("/"):
                    part = part.strip()
                    if part and part not in retailers:
                        retailers.append(part)
            out = "; next, ".join(spoken)
            if retailers:
                out += f". Available at {' and '.join(retailers)}."
            return out + " Tell me the style or brand you want and I'll check more."

        spoken = [_describe_material(it) for it in items[:5]]
        return "; next, ".join(spoken[:3]) + (
            f". Available at {items[0].get('store')}." if items and items[0].get("store") else ""
        ) + (" Tell me the style or brand you want and I'll check more." if len(items) > 3 else "")
    if name == "create_deposit_link":
        if result.get("ok"):
            return f"Deposit link ready: {result.get('url')}."
        return str(result.get("error") or "The deposit link couldn't be created right now.")
    if name == "list_leads":
        leads = result.get("leads") or []
        if not leads:
            return "No leads found."
        lines = [f"{l.get('name', 'Client')} — {l.get('project_type') or l.get('status') or 'lead'} — {l.get('status')} (lead #{l.get('id')})" for l in leads[:8]]
        return "Leads: " + "; ".join(lines)
    if name == "update_lead_status":
        if result.get("ok"):
            return f"Lead #{result.get('lead_id')} is now {result.get('status')}."
        return str(result.get("error") or "Couldn't update that lead.")
    if name == "lookup_permits":
        permits = result.get("permits") or []
        if not permits:
            return "No permits found for that search."
        lines = [f"{p.get('permit_number') or p.get('address') or 'Permit'} — {p.get('work_type') or p.get('job_description') or ''}" for p in permits[:5]]
        return "Permits: " + "; ".join(lines)
    if name == "list_crew":
        crew = result.get("crew") or []
        if not crew:
            return "No crew on file."
        lines = [f"{c.get('name')} — {c.get('role')}" for c in crew[:8]]
        return "Crew: " + "; ".join(lines)
    if name == "lookup_crew_timesheets":
        rows = result.get("timesheets") or []
        if not rows:
            return "No timesheets found."
        lines = [f"{t.get('crew_name') or 'Crew member'} — {t.get('work_date')} {t.get('hours')}h ({t.get('status')})" for t in rows[:6]]
        return "Timesheets: " + "; ".join(lines)
    if name == "get_accounting_summary":
        if result.get("ok"):
            return (f"Collected ${result.get('total_collections_cents', 0) / 100:,.0f}, "
                    f"deposits ${result.get('collected_deposits_cents', 0) / 100:,.0f}, "
                    f"payroll paid ${result.get('payroll_paid_total_cents', 0) / 100:,.0f}.")
        return str(result.get("error") or "Couldn't pull accounting right now.")
    if name == "get_business_summary":
        if result.get("ok"):
            return f"{result.get('total_leads')} total leads, {result.get('open_leads')} open, {result.get('deposits_collected') or 0} deposits collected."
        return str(result.get("error") or "Couldn't pull the summary right now.")
    if name == "make_outbound_call":
        if result.get("ok"):
            return f"I'm calling {result.get('to')} now."
        return str(result.get("error") or "I couldn't place that call right now.")
    if name == "lookup_property":
        if not result.get("ok"):
            if result.get("configured") is False:
                return "I don't have property records hooked up yet — could you tell me the approximate square footage instead?"
            return str(result.get("error") or "I couldn't pull that property right now.")
        parts = [f"{result.get('address') or 'That property'}"]
        if result.get("sqft"):
            parts.append(f"about {result['sqft']:,.0f} square feet")
        if result.get("beds"):
            parts.append(f"{result['beds']:,.0f} bedrooms")
        if result.get("baths"):
            parts.append(f"{result['baths']:,.0f} baths")
        if result.get("year_built"):
            parts.append(f"built {result['year_built']:,.0f}")
        return ". ".join(parts) + "."
    if name == "quote_project":
        if not result.get("ok"):
            return str(result.get("error") or "I couldn't pull that quote right now.")
        words = [f"Ballpark for {result.get('label') or result.get('project_type')}"]
        words.append(f"({result.get('quantifier') or 'project'})")
        words.append(f"is ${result.get('low_est', 0):,.0f} to ${result.get('high_est', 0):,.0f}")
        if result.get("material_notes"):
            words.append("based on " + ", ".join(result["material_notes"]))
        if result.get("assumed_sqft"):
            words.append("— I assumed a typical ~1,750 square foot home since I didn't have the property size")
        if result.get("needs_sqft"):
            words.append("If you can give me the approximate square footage I'll tighten that range right away.")
        words.append("Final price comes from a free on-site walkthrough.")
        return " ".join(words)
    if name == "get_inventory":
        items = result.get("items") or []
        if not items:
            return f"No {result.get('company', '')} inventory on record."
        lines = [f"{i.get('item_name')}: {i.get('qty_on_hand'):g} {i.get('unit') or ''} on hand" + (" (reorder!)" if i.get("low") else "") for i in items[:10]]
        return f"{result.get('company')} supplies — " + "; ".join(lines)
    if name == "update_inventory":
        if result.get("ok"):
            return f"{result.get('item')} is now {result.get('qty_on_hand'):g}" + (" — at reorder level, time to restock" if result.get("low") else "") + "."
        return str(result.get("error") or "Couldn't update inventory.")
    if name == "reorder_report":
        items = result.get("items") or []
        if not items:
            return "Nothing's at the reorder line right now."
        lines = [f"{i.get('item_name')}: {i.get('qty_on_hand'):g} {i.get('unit') or ''} (reorder at {i.get('reorder_level'):g})" for i in items[:10]]
        return "Restock list: " + "; ".join(lines)
    if name == "find_people":
        if not result.get("ok"):
            return str(result.get("error") or "No one found.")
        lines = [f"{p.get('name')} — {p.get('kind')} ({p.get('company')})" + (f" — {p.get('phone')}" if p.get("phone") else "") for p in result["people"][:6]]
        return "Found: " + "; ".join(lines)
    if name == "navigation_guide":
        pages = result.get("pages") or []
        if not pages:
            return "I can walk you through any of our sites — what would you like?"
        return "Here's how to get around: " + " ".join(pages[:4])
    return json.dumps(result, default=str, ensure_ascii=False)


@app.api_route("/api/voice/tool", methods=["POST"])
async def voice_tool(request: Request, db=Depends(get_db)):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    function_name = payload.get("function") or payload.get("function_name")
    argument = payload.get("argument")
    args = None
    if isinstance(argument, dict):
        parsed = argument.get("parsed")
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            args = parsed[0]
        if args is None:
            raw = (argument.get("raw") or "").strip()
            if raw:
                try:
                    loaded = json.loads(raw)
                    args = loaded if isinstance(loaded, dict) else None
                except Exception:
                    args = None

    print(f"VOICE-TOOL call={function_name} args={json.dumps(args) if args is not None else None}", flush=True)

    if not function_name or function_name not in VOICE_ALLOWED_TOOLS:
        return JSONResponse({"response": "I'm not able to do that yet, but the team will follow up by text."})
    if not isinstance(args, dict):
        return JSONResponse({"response": "I didn't catch the details. Could you repeat that?"})

    try:
        handlers = build_tool_handlers(db, stripe_svc)
        result = handlers[function_name](**args)
    except TypeError as e:
        print(f"⚠️ VOICE-TOOL bad args: {e}", flush=True)
        return JSONResponse({"response": "I didn't catch all the details. Could you repeat that?"})
    except Exception as e:
        print(f"⚠️ VOICE-TOOL error: {e}", flush=True)
        return JSONResponse({"response": "That hit a snag — I'll have the team follow up by text."})

    text = _swaig_tool_response_text(function_name, result)
    print(f"VOICE-TOOL result={text[:400]}", flush=True)
    return JSONResponse({"response": text})


@app.api_route("/api/voice/debug", methods=["GET", "POST"])
async def voice_debug(request: Request, db=Depends(get_db)):
    raw = await request.body()
    try:
        data = json.loads(raw or b"{}")
    except Exception:
        data = {"raw": raw.decode("utf-8", "replace")[:4000]}
    if not isinstance(data, dict):
        data = {"raw": json.dumps(data, default=str)[:4000]}
    print(f"VOICE-DEBUG {json.dumps(data, default=str)[:4000]}", flush=True)

    actions = data.get("action")
    if actions and "fetch_conversation" in (actions if isinstance(actions, list) else [actions]):
        return JSONResponse({"conversation_summary": None})

    call_id = (data.get("call_id") or data.get("ai_session_id") or data.get("conversation_id")) if isinstance(data, dict) else None
    agent_summary = (data.get("summary") or "").strip() if isinstance(data, dict) else ""
    call_log = data.get("call_log") if isinstance(data, dict) else None
    if agent_summary or call_log:
        transcript = None
        if isinstance(call_log, list):
            lines = []
            for entry in call_log:
                if not isinstance(entry, dict) or not entry.get("content"):
                    continue
                label = {"assistant": "AI", "user": "Caller", "tool": "Tool", "system": "System"}.get(entry.get("role"), str(entry.get("role")))
                lines.append(f"{label}: {entry['content']}")
            transcript = "\n".join(lines)
        message = agent_summary or transcript or "Voice call (no transcript)"
        if call_id:
            message = f"[session {call_id}]\n" + message
        try:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice', %s, %s, %s);",
                    (data.get("from") or data.get("From") or "unknown",
                     data.get("to") or data.get("To") or company()["phone_e164"], message),
                )
            db.commit()
        except Exception as e:
            print(f"⚠️ VOICE-DEBUG log insert failed: {e}", flush=True)
    return Response(content="", status_code=204)


# --- Instant ballpark quote -------------------------------------------------
INSTANT_PROJECT_TYPES = [
    "Whole-Home Renovation", "Interior Refresh", "Kitchen", "Bathroom",
    "Roofing & Siding", "Drywall & Paint", "Deck & Fence", "Short-Term Rental Make-Ready",
]


@app.get("/instant-quote", response_class=HTMLResponse)
async def instant_quote_page(request: Request):
    return templates.TemplateResponse(request=request, name="instant_quote.html", context={
        "project_types": INSTANT_PROJECT_TYPES,
        "error": request.query_params.get("error"),
        "address": request.query_params.get("address", ""),
        "project_type": request.query_params.get("project_type", ""),
        "quote": None, "prop": None, "api_configured": property_service.is_configured(),
    })


@app.get("/api/property")
async def property_lookup(address: str = ""):
    prop = property_service.lookup_address(address)
    if not prop:
        return JSONResponse(content={"source": "manual"})
    return JSONResponse(content=prop)


@app.post("/instant-quote", response_class=HTMLResponse)
async def instant_quote_submit(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    project_type: str = Form(""),
    sqft: str = Form(""),
    db=Depends(get_db),
):
    ctx = {
        "project_types": INSTANT_PROJECT_TYPES,
        "address": address, "project_type": project_type,
        "name": name, "phone": phone, "email": email, "sqft": sqft,
        "error": None, "quote": None, "prop": None, "api_configured": property_service.is_configured(),
    }
    email = (email or "").strip()
    if not email:
        ctx["error"] = "Please add your email — we'll send your quote there."
        return templates.TemplateResponse(request=request, name="instant_quote.html", context=ctx)
    prop = property_service.lookup_address(address)
    manual_sqft = 0
    try:
        manual_sqft = float(sqft or 0)
    except ValueError:
        manual_sqft = 0

    est = None
    if prop and prop.get("sqft"):
        est = estimating_service.estimate(project_type, prop, 0)
        ctx["prop"] = prop
    elif manual_sqft and manual_sqft > 0:
        est = estimating_service.estimate(project_type, None, manual_sqft)
    if est is None:
        ctx["error"] = "We couldn't auto-detect this property. Please enter your square footage to continue."
        return templates.TemplateResponse(request=request, name="instant_quote.html", context=ctx)

    lead_id = None
    created = False
    try:
        res = lead_intake.intake_lead(
            db, company="construction", source="instant_quote", name=name,
            phone=phone, email=email, address=address, project_type=project_type,
            description=f"Instant ballpark {est['label']} on a {est['sqft']:,} sq ft property.",
            extra={
                "property_sqft": est["sqft"] or None,
                "estimate_low_cents": est["low_cents"],
                "estimate_high_cents": est["high_cents"],
                "estimate_json": json.dumps(est),
            },
        )
        lead_id, created = res["lead_id"], res["created"]
    except Exception as e:
        print(f"⚠️ instant quote lead save failed: {e}", flush=True)

    if not lead_id:
        # No lead saved: leave the on-page quote in place so they can retry or
        # use /get-started instead.
        return templates.TemplateResponse(request=request, name="instant_quote.html", context={**ctx, "error": "We hit a glitch saving your request — please try again or use the get-started form."})

    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=name, phone=phone, email=email,
            service=project_type, address=address, source="instant_quote",
        )
    except Exception as e:
        print(f"⚠️ instant quote owner notify failed: {e}", flush=True)

    if created:
        # A merged/duplicate quote request already has a bot draft and a
        # follow-up scheduled; staging a second one would double send.
        try:
            auto_reply.auto_reply_to_lead(
                db, "construction", name=name, phone=phone, email=email,
                service=project_type, address=address,
                message=f"Instant ballpark {est['label']} on a {est['sqft']:,} sq ft property.",
                source="instant_quote", sqft=est.get("sqft"), lead_id=lead_id,
            )
        except Exception as e:
            print(f"⚠️ instant quote auto-reply failed: {e}", flush=True)

        try:
            _follow_up_new_lead(lead_id)
        except Exception as e:
            print(f"⚠️ instant quote follow-up failed: {e}", flush=True)

    ctx.update({"quote": est, "lead_id": lead_id, "prop": prop})
    return templates.TemplateResponse(request=request, name="instant_quote.html", context=ctx)


PROJECT_TYPE_CHOICES = [
    "Kitchen remodel",
    "Bathroom remodel",
    "Whole-home renovation",
    "Additions",
    "Roofing",
    "Siding / windows",
    "Deck / porch",
    "Flooring",
    "Painting",
    "Foundation / structural",
    "Garage / ADU",
    "Other",
]


# --- Embeddable quote widget ------------------------------------------------
# A chrome-free version of the instant-quote form for an <iframe> on any site.
# Same intake and same guardrails as /instant-quote -- it still lands in `leads`,
# so auto_reply and the follow-up dial behave identically. Only the rendering
# differs, which is why it reuses that route's handlers rather than duplicating
# the estimate logic.
_EMBED_TYPES = [
    "Kitchen remodel", "Bathroom remodel", "Whole-home renovation", "Additions",
    "Roofing", "Siding / windows", "Deck / porch", "Flooring", "Painting",
    "Foundation / structural", "Garage / ADU", "Other",
]


@app.get("/embed/quote", response_class=HTMLResponse)
async def embed_quote_page(request: Request):
    return templates.TemplateResponse(request=request, name="embed_quote.html", context={
        "project_types": _EMBED_TYPES,
        "error": request.query_params.get("error"),
        "name": request.query_params.get("name", ""),
        "phone": request.query_params.get("phone", ""),
        "email": request.query_params.get("email", ""),
        "address": request.query_params.get("address", ""),
        "project_type": request.query_params.get("project_type", ""),
        "sqft": request.query_params.get("sqft", ""),
        "posted": False,
    })


@app.post("/embed/quote", response_class=HTMLResponse)
async def embed_quote_submit(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    address: str = Form(""),
    project_type: str = Form(""),
    sqft: str = Form(""),
    company_website: str = Form(""),
    db=Depends(get_db),
):
    def back(error=None, code=200):
        return templates.TemplateResponse(
            request=request, name="embed_quote.html",
            context={
                "project_types": _EMBED_TYPES, "error": error, "name": name,
                "phone": phone, "email": email, "address": address,
                "project_type": project_type, "sqft": sqft, "posted": code == 200,
            },
            status_code=code,
        )

    email = (email or "").strip()
    if _is_form_bot(company_website, "embed_quote"):
        return back(None, 200)
    if not email:
        return back("Please add your email — we'll send your quote there.", 422)

    prop = property_service.lookup_address(address)
    manual_sqft = 0
    try:
        manual_sqft = int(re.sub(r"[^0-9]", "", sqft or "0") or 0)
    except ValueError:
        manual_sqft = 0

    try:
        est = estimating_service.auto_quote(project_type or "", manual_sqft or None)
    except Exception as e:
        print(f"⚠️ embed quote estimate failed: {e}", flush=True)
        est = estimating_service.auto_quote(project_type or "", None)

    try:
        lead_id = lead_intake.intake_lead(
            db, company="construction", source="embed_quote", name=name,
            phone=phone, email=email, address=address,
            description=f"Embed widget: {project_type}",
            project_type=project_type,
            extra={"budget": "", "timeline": "", "sqft": sqft},
        )["lead_id"]
    except Exception as e:
        print(f"⚠️ embed quote intake failed: {e}", flush=True)
        return back("We hit a glitch saving your request — please try again.", 500)
    await asyncio.to_thread(zoho_sync.after_intake, db, lead_id)

    # Same notifications as the full form. Guarded individually so one failure
    # does not cost the lead.
    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=name, phone=phone, email=email,
            service=project_type, address=address, budget="", timeline="",
            message="Embed widget request", source="embed_quote",
        )
    except Exception as e:
        print(f"⚠️ embed quote owner notify failed: {e}", flush=True)
    try:
        auto_reply.auto_reply_to_lead(
            db, "construction", name=name, phone=phone, email=email,
            service=project_type, address=address, message="Embed widget request",
            source="embed_quote", sqft=est.get("sqft"), lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ embed quote auto-reply failed: {e}", flush=True)

    return back(None, 200)


@app.get("/embed/snippet", response_class=HTMLResponse)
async def embed_snippet(request: Request):
    """Returns a copy-pasteable iframe tag with this deployment's own domain."""
    return JSONResponse(content={
        "iframe": (
            '<iframe src="/embed/quote" title="Get a construction quote" '
            'style="width:100%;max-width:460px;height:620px;border:0;border-radius:12px" '
            'loading="lazy"></iframe>'
        ),
        "note": (
            "Relative URL renders on this deployment. For another domain use the "
            "absolute origin, e.g. https://construction.bizstackperks.com/embed/quote"
        ),
        "height_hint": "620px on desktop; bump to 720px if the iframe is narrower than 400px",
    })


# --- Public acquisition funnel (+ referral attribution) ----------------------
@app.get("/get-started", response_class=HTMLResponse)
async def get_started_page(request: Request):
    return templates.TemplateResponse(request=request, name="get_started.html", context={
        "project_types": PROJECT_TYPE_CHOICES,
        "ref": request.query_params.get("ref", ""),
    })


@app.post("/get-started", response_class=HTMLResponse)
async def get_started_submit(
    request: Request,
    full_name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    project_type: str = Form(""),
    address: str = Form(""),
    budget: str = Form(""),
    timeline: str = Form(""),
    message: str = Form(""),
    ref: str = Form(""),
    company_website: str = Form(""),
    db=Depends(get_db),
):
    if _is_form_bot(company_website, "get-started"):
        return RedirectResponse(url="/get-started?sent=1", status_code=status.HTTP_303_SEE_OTHER)
    name = (full_name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()
    typed = dict(full_name=name, phone=phone, email=email, address=address, message=message)
    if not name or not phone:
        return _form_error_redirect("/get-started", "Please add your name and phone", **typed)
    if not email:
        return _form_error_redirect("/get-started", "Please add your email — we'll send your quote there", **typed)

    ref_code = (ref or "").strip().lower()[:50]
    partner_id = None
    if ref_code:
        try:
            with db.cursor() as cur:
                cur.execute("SELECT id FROM partners WHERE referral_code = %s;", (ref_code,))
                prow = cur.fetchone()
                if prow:
                    partner_id = prow["id"]
                else:
                    cur.execute("SELECT id FROM referral_codes WHERE code = %s;", (ref_code,))
                    if not cur.fetchone():
                        ref_code = ""
        except Exception as e:
            print(f"⚠️ get-started ref lookup failed: {e}", flush=True)
            ref_code = ""

    try:
        res = lead_intake.intake_lead(
            db, company="construction", source="acquisition", name=name,
            phone=phone, email=(email or "").strip(), address=(address or "").strip(),
            description=(message or "").strip()[:2000], project_type=project_type,
            extra={
                "budget": (budget or "").strip(),
                "timeline": (timeline or "").strip(),
                "referral_code": ref_code or None,
                "partner_id": partner_id,
                "campaign": "get-started",
            },
        )
        lead_id = res["lead_id"]
    except Exception as e:
        print(f"⚠️ get-started lead save failed: {e}", flush=True)
        return _form_error_redirect("/get-started", "Something went wrong — please try again or call us directly", **typed)
    await asyncio.to_thread(zoho_sync.after_intake, db, lead_id)

    summary = (
        f"🛠 New acquisition lead (#{lead_id}) — {name}, {phone}"
        + (f", {email.strip()}" if (email or "").strip() else "")
        + (f"\nProject: {project_type}" if project_type else "")
        + (f"\nAddress: {address}" if address else "")
        + (f"\nBudget: {budget}" if budget else "")
        + (f"\nTimeline: {timeline}" if timeline else "")
        + (f"\n{message[:200]}" if (message or "").strip() else "")
        + (f"\nRef: {ref_code}" if ref_code else "")
    )
    if os.getenv("OWNER_SMS_ENABLED", "0").lower() in ("1", "true", "yes"):
        try:
            signalwire.send_sms(os.getenv("SIGNALWIRE_PHONE", "+17578469275"), summary[:1500])
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    ("outbound", "sms", "system", company()["phone_e164"], summary),
                )
                db.commit()
        except Exception as e:
            print(f"⚠️ get-started owner SMS failed: {e}", flush=True)

    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=name, phone=phone, email=(email or "").strip(),
            service=(project_type or ""), address=(address or ""),
            budget=(budget or ""), timeline=(timeline or ""),
            message=(message or ""), source="acquisition",
        )
    except Exception as e:
        print(f"⚠️ get-started owner email failed: {e}", flush=True)

    try:
        auto_reply.auto_reply_to_lead(
            db, "construction", name=name, phone=phone, email=email,
            service=(project_type or ""), address=(address or ""),
            budget=(budget or ""), timeline=(timeline or ""),
            message=(message or ""), source="acquisition",
            lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ get-started auto-reply failed: {e}", flush=True)

    try:
        _follow_up_new_lead(lead_id)
    except Exception as e:
        print(f"⚠️ get-started follow-up failed: {e}", flush=True)

    return RedirectResponse(url="/get-started?sent=1", status_code=status.HTTP_303_SEE_OTHER)


# --- Admin: acquisition radar (LinkedIn + Reddit) ---------------------------
_radar_jobs = {}
_radar_jobs_lock = threading.Lock()


def _radar_status(source: str) -> dict:
    with _radar_jobs_lock:
        return dict(_radar_jobs.get(source, {}))


def _run_radar_scan(source: str, scan_fn):
    with _radar_jobs_lock:
        if _radar_jobs.get(source, {}).get("running"):
            return False
        _radar_jobs[source] = {"running": True, "started_at": datetime.utcnow(), "created": 0, "seen": 0, "errors": ""}
    threading.Thread(target=_radar_worker, args=(source, scan_fn), daemon=True).start()
    return True


def _radar_worker(source: str, scan_fn):
    created = 0
    seen = 0
    errors = []
    try:
        result = scan_fn()
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                for m in result.get("matches", []):
                    cur.execute("SELECT 1 FROM lead_posts WHERE post_id = %s;", (m["post_id"],))
                    if cur.fetchone():
                        seen += 1
                        continue
                    meta = json.dumps({"title": m["title"], "subreddit": m["subreddit"]})
                    cur.execute(
                        "INSERT INTO leads (name, email, listing_url, status, source, funding_needed, funding_use, referral_code, campaign, analysis_json, company) "
                        "VALUES (%s, %s, %s, 'new', %s, %s, %s, %s, 'construction-radar', %s, 'construction') RETURNING id;",
                        (
                            f"{source.title()} · {m['author']}",
                            f"con-{source}-{m['post_id']}@lead.local",
                            m["permalink"],
                            source,
                            m["funding"],
                            (m.get("funding_use") or "")[:500],
                            m["post_id"],
                            meta,
                        ),
                    )
                    lead_id = cur.fetchone()["id"]
                    cur.execute(
                        "INSERT INTO lead_posts (post_id, subreddit, created_utc, lead_id) VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (post_id) DO NOTHING;",
                        (m["post_id"], m["subreddit"], m["created_utc"], lead_id),
                    )
                    created += 1
                conn.commit()
        errors = list(result.get("errors", []))
    except Exception as exc:
        errors.append(f"scan failed: {exc}"[:280])
    print(f"[con-radar {source}] created={created} seen={seen} errors={errors}", flush=True)
    with _radar_jobs_lock:
        _radar_jobs[source] = {
            "running": False,
            "created": created,
            "seen": seen,
            "errors": "; ".join(errors)[:300],
        }


# --- Public lead sources (bid/RFP boards) -----------------------------------
_lead_source_status = {
    "running": False, "last_run": None, "found": 0, "created": 0, "pushed": 0, "errors": "",
}
_lead_source_lock = threading.Lock()


def _ingest_public_source_leads(matches):
    """Dedupe by (source, external_id), insert new leads, return created rows."""
    created = []
    seen = 0
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for m in matches:
                src = m.get("source") or "public"
                ext = str(m.get("external_id") or "")
                if not ext:
                    continue
                cur.execute(
                    "SELECT 1 FROM lead_source_seen WHERE source = %s AND external_id = %s;",
                    (src, ext),
                )
                if cur.fetchone():
                    seen += 1
                    continue
                contact_name = (m.get("contact_name") or "").strip()
                name = (m.get("title") or "Government bid").strip()
                if contact_name:
                    name = f"{name} · {contact_name}"
                cur.execute(
                    "INSERT INTO leads (name, phone, email, project_type, address, description, source, status, company, listing_url, campaign) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, 'new', 'construction', %s, 'lead-source-scan') RETURNING id;",
                    (
                        name[:250], m.get("phone") or "", (m.get("email") or "").strip(),
                        (m.get("service") or "")[:120], (m.get("address") or "")[:250],
                        (m.get("description") or "")[:2000], src, (m.get("url") or "")[:512],
                    ),
                )
                lead_id = cur.fetchone()["id"]
                cur.execute(
                    "INSERT INTO lead_source_seen (source, external_id, lead_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (source, external_id) DO NOTHING;",
                    (src, ext, lead_id),
                )
                created.append({
                    "lead_id": lead_id, "name": name, "phone": m.get("phone") or "",
                    "email": (m.get("email") or "").strip(), "service": m.get("service") or "",
                    "address": m.get("address") or "", "source": src, "title": m.get("title") or "",
                    "contact_name": contact_name, "url": m.get("url") or "",
                })
        conn.commit()
    return created, seen


def run_lead_source_scan():
    with _lead_source_lock:
        if _lead_source_status.get("running"):
            return {"status": "already_running"}
        _lead_source_status.update({"running": True, "errors": ""})
    started = datetime.utcnow()
    found = created = pushed = 0
    errors = ""
    try:
        result = lead_sources.scan(preset="construction")
        matches = result.get("matches", [])
        found = len(matches)
        rows, _seen = _ingest_public_source_leads(matches)
        created = len(rows)
        auto_contact = (os.getenv("LEAD_SOURCE_AUTO_CONTACT", "") or "").strip().lower() in ("1", "true", "yes", "on")
        auto_email = (os.getenv("LEAD_SOURCE_AUTO_EMAIL", "") or "").strip().lower() in ("1", "true", "yes", "on")
        try:
            inquiry_cap = max(0, int(os.getenv("LEAD_INQUIRY_MAX_PER_RUN", "15") or 15))
        except (TypeError, ValueError):
            inquiry_cap = 15
        try:
            inquiry_delay = max(0.0, float(os.getenv("LEAD_INQUIRY_DELAY", "1.5") or 1.5))
        except (TypeError, ValueError):
            inquiry_delay = 1.5
        inquiries = 0
        push_db = psycopg.connect(db_url, row_factory=dict_row)
        push_db.autocommit = True
        try:
            # Machine-sourced leads go straight to outreach below; the owner is
            # not emailed one message per scraped row. The results are on the
            # Today screen, and a lead that answers is what deserves attention.
            for r in rows:
                if auto_email and r.get("email") and inquiries < inquiry_cap:
                    try:
                        if auto_reply.send_bid_inquiry(
                            push_db, "construction", title=r["title"], contact_name=r.get("contact_name", ""),
                            email=r["email"], service=r["service"], address=r["address"],
                            url=r.get("url", ""), lead_id=r["lead_id"],
                        ):
                            pushed += 1
                            inquiries += 1
                            if inquiry_delay > 0:
                                time.sleep(inquiry_delay)
                    except Exception as exc:
                        print(f"[lead-source] bid inquiry failed for {r['lead_id']}: {exc}", flush=True)
                if not auto_contact:
                    continue
                try:
                    auto_reply.auto_reply_to_lead(
                        push_db, "construction", name=r["name"], phone=r["phone"],
                        email=r["email"], service=r["service"], address=r["address"],
                        message=r["title"], source=r["source"], lead_id=r["lead_id"],
                    )
                except Exception as exc:
                    print(f"[lead-source] auto-reply failed for {r['lead_id']}: {exc}", flush=True)
                try:
                    _follow_up_new_lead(r["lead_id"])
                except Exception as exc:
                    print(f"[lead-source] follow-up failed for {r['lead_id']}: {exc}", flush=True)
            # The "copy each draft into your email and send" digest is gone: it
            # made the owner the relay for mail the bot had already written.
            # send_bid_inquiry / auto_reply_to_lead above deliver it directly,
            # inside auto_reply's caps and contact policy.
            if rows:
                print(f"[lead-source] {len(rows)} lead(s) processed, {pushed} inquiries sent", flush=True)
        finally:
            try:
                push_db.close()
            except Exception:
                pass
        errors = "; ".join(result.get("errors", []))[:300]
    except Exception as exc:
        errors = str(exc)[:300]
        print(f"[lead-source] scan failed: {exc}", flush=True)
    with _lead_source_lock:
        _lead_source_status.update({
            "running": False, "last_run": started.isoformat(), "found": found,
            "created": created, "pushed": pushed, "errors": errors,
        })
    print(f"[lead-source] found={found} created={created} pushed={pushed} {errors or 'ok'}", flush=True)
    return {"status": "ok", "found": found, "created": created, "pushed": pushed, "errors": errors}


def pipeline_digest():
    """Env-gated boot report (LEAD_PIPELINE_DIGEST=1): one JSON print of the
    whole shared lead pipeline across companies."""
    try:
        vis = source_visibility_clause()
        vis_params = tuple(source_visibility_params())
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source, company, status, COUNT(*) AS c FROM leads WHERE 1=1"
                    + vis + " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;",
                    vis_params,
                )
                by_source = cur.fetchall()
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE source IN ('website','referral') AND email IS NOT NULL "
                    "  AND email <> '' AND email NOT LIKE '%@lead.local') AS website_real_email, "
                    "COUNT(*) FILTER (WHERE source IN ('website','referral')) AS website_total, "
                    "COUNT(*) FILTER (WHERE source IN ('website','referral') AND status = 'analyzed') AS website_analyzed, "
                    "COUNT(*) FILTER (WHERE source IN ('website','referral') AND status IN ('quoted','deposit','in_progress')) AS website_active, "
                    "COUNT(*) FILTER (WHERE source IN ('website','referral') AND last_reply_at IS NOT NULL) AS website_replied "
                    "FROM leads;"
                )
                website = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) AS scan_pending FROM leads WHERE campaign = 'lead-source-scan' AND status = 'new' "
                    "AND email IS NOT NULL AND email <> '' AND email NOT LIKE '%@lead.local' "
                    "AND NOT EXISTS (SELECT 1 FROM comms_logs cl WHERE cl.channel = 'email' AND cl.direction = 'outbound' "
                    "  AND LOWER(cl.recipient) = LOWER(leads.email));"
                )
                scan_pending = cur.fetchone()["scan_pending"]
                cur.execute(
                    "SELECT COUNT(*) AS overdue FROM leads WHERE email IS NOT NULL AND email <> '' "
                    "AND email NOT LIKE '%@lead.local' AND last_reply_at IS NULL "
                    "AND source IN ('website','referral') "
                    "AND EXISTS (SELECT 1 FROM comms_logs cl WHERE cl.channel = 'email' AND cl.direction = 'outbound' "
                    "  AND LOWER(cl.recipient) = LOWER(leads.email)) "
                    "AND NOT EXISTS (SELECT 1 FROM leads l2 WHERE l2.id = leads.id AND status IN ('quoted','deposit','in_progress','completed','lost'));"
                )
                overdue = cur.fetchone()["overdue"]
                cur.execute(
                    "SELECT COUNT(*) FILTER (WHERE email LIKE '%@lead.local') AS masked, "
                    "COUNT(*) FILTER (WHERE source = 'reddit') AS reddit, "
                    "COUNT(*) FILTER (WHERE source = 'permit_finder') AS permits "
                    "FROM leads;"
                )
                masked = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) AS callable FROM leads WHERE source IN ('website','referral') "
                    "AND status IN ('new','analyzed','contacted') "
                    "AND length(regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g')) >= 10;"
                )
                callable = cur.fetchone()["callable"]
                cur.execute(
                    "SELECT id, name, status, project_type, company FROM leads "
                    "WHERE status IN ('quoted','deposit','in_progress') ORDER BY created_at DESC;"
                )
                active_jobs = cur.fetchall()
        return {
            "by_source": by_source,
            "website": website,
            "scan_pending": scan_pending,
            "website_overdue_no_reply": overdue,
            "masked_reddit_permits": masked,
            "website_callable": callable,
            "active_jobs": active_jobs,
        }
    except Exception as exc:
        return {"error": str(exc)[:400]}


def _gmail_inbound_loop():
    """Collect lead replies from the connected Google mailbox via the Gmail API."""
    interval = max(120, int(os.getenv("GMAIL_INBOUND_INTERVAL_SECONDS", "900") or 900))
    while True:
        try:
            with psycopg.connect(db_url, row_factory=dict_row) as conn:
                res = inbound_email.poll_gmail_inbox(
                    conn, "construction", "construction",
                    os.getenv("SMTP_FROM", "") or "hello@bizstackperks.com",
                )
            if res.get("error"):
                print(f"[gmail-inbound] {res['error']}", flush=True)
            elif res.get("inserted"):
                print(f"[gmail-inbound] {res}", flush=True)
        except Exception as exc:
            print(f"[gmail-inbound] poll failed: {exc}", flush=True)
        time.sleep(interval)


def _zoho_imap_loop():
    """Read homeowner replies out of the Zoho mailbox.

    MX for the domain points at Zoho, so replies land there. The Gmail poller
    reads a different mailbox and therefore cannot see them. IMAP on 993 is
    reachable from this container and the mailbox password authenticates; the
    only prerequisite is the IMAP Access toggle in Zoho Mail settings, so when
    login answers "enable IMAP" this backs off for an hour instead of looping.
    """
    interval = max(300, int(os.getenv("ZOHO_IMAP_INTERVAL_SECONDS", "900") or 900))
    backoff = interval
    while True:
        try:
            import zoho_imap
            import zoho_mail
            # The Mail REST API needs no IMAP toggle and uses the scope the
            # connection already carries; IMAP stays as the fallback.
            if zoho_mail.available() and zoho_mail.inbox_status().get("ok"):
                backoff = interval
                with psycopg.connect(db_url, row_factory=dict_row) as conn:
                    res = zoho_mail.poll_inbox(
                        conn, "construction",
                        os.getenv("SMTP_FROM", "") or "hello@bizstackperks.com")
                if res.get("error"):
                    print(f"[zoho-mail] {res['error']}", flush=True)
                elif res.get("inserted"):
                    print(f"[zoho-mail] {res}", flush=True)
                time.sleep(backoff)
                continue
            st = zoho_imap.status(force=True)
            if st.get("ok"):
                backoff = interval
                with psycopg.connect(db_url, row_factory=dict_row) as conn:
                    res = zoho_imap.poll_zoho_inbox(
                        conn, "construction",
                        os.getenv("SMTP_FROM", "") or "hello@bizstackperks.com")
                if res.get("error"):
                    print(f"[zoho-imap] {res['error']}", flush=True)
                elif res.get("inserted"):
                    print(f"[zoho-imap] {res}", flush=True)
            else:
                backoff = max(3600, interval)
                print(f"[zoho-imap] mailbox not readable yet: {st.get('reason')}", flush=True)
        except Exception as exc:
            backoff = max(3600, interval)
            print(f"[zoho-imap] poll failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(backoff)


def _call_list_loop():
    """Once a day, at CALL_LIST_HOUR, build tomorrow's list and text the link.

    Sleeping until the hour rather than every N minutes keeps it to one text a
    day per person: a loop that re-runs on restart would otherwise text Ryan
    again every deploy. `assign` is idempotent, so a restart inside the window
    tops the list up instead of duplicating it.
    """
    try:
        hour = max(0, min(int(os.getenv("CALL_LIST_HOUR", "16") or 16), 23))
    except (TypeError, ValueError):
        hour = 16
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            time.sleep(max(60, (target - now).total_seconds()))
            if not db_url:
                continue
            with psycopg.connect(db_url, row_factory=dict_row) as conn:
                res = call_list.auto_plan(conn, "construction")
            print(f"[call-list] {res}", flush=True)
        except Exception as exc:
            print(f"[call-list] plan failed: {type(exc).__name__}: {exc}", flush=True)
            time.sleep(1800)


def _start_lead_source_scheduler():
    try:
        interval_h = max(0.5, float(os.getenv("LEAD_SCAN_INTERVAL_HOURS", "6") or 6))
    except (TypeError, ValueError):
        interval_h = 6

    # Tomorrow's call list, built and texted at a fixed hour. Off unless
    # CALL_LIST_CREW_IDS names someone, because an unrequested daily text is
    # worse than no text. This is the last manual step in the pipeline: the bot
    # writes, answers and books, and a person still has to ring people.
    if call_list.crew_ids_from_env():
        try:
            threading.Thread(target=_call_list_loop, daemon=True).start()
            print(f"[call-list] loop started for crew {call_list.crew_ids_from_env()}", flush=True)
        except Exception as exc:
            print(f"[call-list] could not start: {exc}", flush=True)

    # Zoho mailbox first: that is where replies actually arrive (MX -> Zoho).
    try:
        import zoho_imap
        if zoho_imap.enabled():
            threading.Thread(target=_zoho_imap_loop, daemon=True).start()
            print("[zoho-imap] loop started", flush=True)
        else:
            print("[zoho-imap] off (no mailbox credentials, or ZOHO_IMAP_INBOUND=off)", flush=True)
    except Exception as exc:
        print(f"[zoho-imap] could not start: {exc}", flush=True)

    # The Gmail path stays: it is still the outbound mailbox and it may hold
    # forwarded mail. It cannot see anything that only reached Zoho.
    gmail_inbound = (os.getenv("GMAIL_INBOUND", "") or "").strip().lower() in ("1", "true", "yes", "on")
    if gmail_inbound:
        threading.Thread(target=_gmail_inbound_loop, daemon=True).start()
        print("[gmail-inbound] loop started", flush=True)

    def _runner():
        print(f"[lead-source] scheduler started (every {interval_h:g}h)", flush=True)
        try:
            time.sleep(float(os.getenv("LEAD_SCAN_START_DELAY", "45") or 45))
        except (TypeError, ValueError):
            time.sleep(45)
        debug_active = (os.getenv("LEAD_DEBUG_ACTIVE", "") or "").strip().lower() in ("1", "true", "yes", "on")
        if debug_active:
            try:
                with psycopg.connect(db_url, row_factory=dict_row) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id, name, phone, email, project_type, status, deposit_status "
                            "FROM leads WHERE status IN ('quoted','deposit','in_progress') "
                            "AND company = 'construction' ORDER BY created_at DESC;"
                        )
                        print(f"[debug-active] rows={cur.fetchall()}", flush=True)
            except Exception as exc:
                print(f"[debug-active] failed: {exc}", flush=True)
        digest_on = (os.getenv("LEAD_PIPELINE_DIGEST", "") or "").strip().lower() in ("1", "true", "yes", "on")
        if digest_on:
            try:
                print(f"[pipeline-digest] {pipeline_digest()}", flush=True)
            except Exception as exc:
                print(f"[pipeline-digest] failed: {exc}", flush=True)
        fire_mode = (os.getenv("LEAD_FIRE_PENDING", "") or "").strip().lower()
        if fire_mode:
            try:
                fire_max = max(0, int(os.getenv("LEAD_FIRE_MAX", "0") or 0))
            except (TypeError, ValueError):
                fire_max = 0
            try:
                result = auto_reply.fire_pending_bid_inquiries(
                    "construction", dry_run=(fire_mode in ("dry", "dryrun", "count")), max_emails=fire_max,
                )
                print(f"[bulk-fire construction] result={result}", flush=True)
            except Exception as exc:
                print(f"[bulk-fire construction] failed: {exc}", flush=True)
        while True:
            try:
                run_lead_source_scan()
            except Exception as exc:
                print(f"[lead-source] loop error: {exc}", flush=True)
            time.sleep(interval_h * 3600)

    threading.Thread(target=_runner, daemon=True).start()


@app.post("/api/email/inbound")
async def email_inbound_webhook(request: Request):
    secret = (os.getenv("INBOUND_EMAIL_SECRET", "") or "").strip()
    if secret:
        # Accept the shared secret however the provider carries it. Resend signs
        # with Svix and sends `svix-signature`; the old check only looked at a
        # `?secret=` query param, so every genuine delivery 401'd and inbound
        # email was never ingested.
        candidates = [
            request.query_params.get("secret"),
            request.headers.get("X-Webhook-Secret"),
            request.headers.get("X-Resend-Secret"),
            request.headers.get("X-Resend-Signature"),
            (request.headers.get("svix-signature") or "").split(" ")[0] or None,
        ]
        if not any(g and hmac.compare_digest(g.strip(), secret) for g in candidates):
            return JSONResponse(content={"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
        result = inbound_email.handle_webhook_payload(
            body, "construction", os.getenv("SMTP_FROM", "") or "hello@bizstackperks.com"
        )
        print(f"[email-inbound] webhook ingested: {result}", flush=True)
        return JSONResponse(content={"ok": True, **result})
    except Exception as exc:
        print(f"[email-inbound] webhook error: {exc}", flush=True)
        return JSONResponse(content={"ok": False, "error": str(exc)[:300]}, status_code=500)


@app.post("/lead-sources/scan")
async def lead_sources_scan(request: Request):
    require_admin(request)
    return JSONResponse(content=run_lead_source_scan())


@app.get("/lead-sources/status")
async def lead_sources_status(request: Request):
    require_admin(request)
    with _lead_source_lock:
        return JSONResponse(content=dict(_lead_source_status))


@app.post("/acquisition/scan/public")
async def acquisition_scan_public(request: Request):
    is_authed, _ = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not _lead_source_status.get("running"):
        threading.Thread(target=run_lead_source_scan, daemon=True).start()
        return RedirectResponse(url="/acquisition?scan_started=Public+bid+boards", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/acquisition?scan_running=public", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/acquisition", response_class=HTMLResponse)
async def acquisition_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM leads WHERE company = 'construction' AND source IN ('reddit', 'linkedin') "
            "ORDER BY created_at DESC LIMIT 200;"
        )
        rows = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE created_at >= date_trunc('month', now())) AS this_month, "
            "COUNT(*) FILTER (WHERE partner_id IS NOT NULL) AS referred, "
            "COUNT(*) FILTER (WHERE status = 'new') AS needs_attention "
            "FROM leads WHERE company = 'construction' AND source = 'acquisition';"
        )
        funnel_stats = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*) AS total, "
            "COUNT(*) FILTER (WHERE status = 'new') AS needs_attention "
            "FROM leads WHERE company = 'construction' AND source IN ('reddit', 'linkedin');"
        )
        radar_stats = cur.fetchone()

    leads = []
    for l in rows:
        meta = {}
        try:
            meta = json.loads(l.get("analysis_json") or "{}")
        except (TypeError, ValueError):
            pass
        source = l.get("source") or "reddit"
        author = (l.get("name") or "").split("·", 1)[-1].strip() or f"{source} user"
        mins = None
        try:
            mins = int((datetime.utcnow() - l["created_at"]).total_seconds() // 60)
        except Exception:
            pass
        leads.append({
            "id": l["id"],
            "author": author,
            "subreddit": meta.get("subreddit", ""),
            "title": meta.get("title", ""),
            "permalink": l.get("listing_url", ""),
            "funding": bool(l.get("funding_needed")),
            "minutes_ago": mins,
            "status": l.get("status", "new"),
            "source": source,
            "draft": construction_radar.outreach_draft({
                "author": author,
                "subreddit": meta.get("subreddit", ""),
                "title": meta.get("title", ""),
                "funding": bool(l.get("funding_needed")),
            }),
        })

    return templates.TemplateResponse(request=request, name="acquisition.html", context={
        "user": {"email": user_email},
        "leads": leads,
        "funnel_stats": funnel_stats,
        "radar_stats": radar_stats,
        "scanned": request.query_params.get("scanned"),
        "errors": request.query_params.get("errors", ""),
        "scan_started": request.query_params.get("scan_started"),
        "scan_running": request.query_params.get("scan_running"),
        "radar_status": {
            "reddit": _radar_status("reddit"),
            "linkedin": _radar_status("linkedin"),
        },
        "lead_source_status": dict(_lead_source_status),
    })


@app.post("/acquisition/scan/reddit")
async def acquisition_scan_reddit(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not _run_radar_scan("reddit", construction_radar.scan_reddit):
        return RedirectResponse(url="/acquisition?scan_running=reddit", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/acquisition?scan_started=reddit", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/acquisition/scan/linkedin")
async def acquisition_scan_linkedin(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not _run_radar_scan("linkedin", construction_radar.scan_linkedin):
        return RedirectResponse(url="/acquisition?scan_running=linkedin", status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url="/acquisition?scan_started=linkedin", status_code=status.HTTP_303_SEE_OTHER)


# --- Public web chat (same AI agent as the phones) --------------------------
WEBCHAT_COOKIE = "bsc_webchat"
WEBCHAT_COOKIE_MAX_AGE = 60 * 60 * 24 * 31  # 31 days, cookie only


def _webchat_session(request: Request) -> tuple:
    """Return (session_key, is_new) for this visitor.

    The key is a conversation bucket, not an auth token -- it grants access to
    nothing but that visitor's own transcript. 192 bits of randomness, so one
    visitor cannot guess another's.
    """
    existing = request.cookies.get(WEBCHAT_COOKIE)
    if existing and len(existing) >= 32:
        return existing, False
    return secrets.token_urlsafe(24), True


def _with_webchat_cookie(response: Response, session_key: str) -> Response:
    response.set_cookie(
        WEBCHAT_COOKIE, session_key, max_age=WEBCHAT_COOKIE_MAX_AGE,
        httponly=True, samesite="lax", path="/",
    )
    return response


@app.post("/api/chat")
async def web_chat(
    request: Request,
    response: Response,
    message: str = Form(...),
    db=Depends(get_db),
):
    session_key, _ = _webchat_session(request)
    history = copilot_memory.load_history(db, "webchat", session_key)
    agent = BusinessAIAgent(
        knowledge_path="construction_knowledge.md",
        tool_handlers=build_tool_handlers(db, stripe_svc),
        subset="guest",
    )
    # The model round-trip is synchronous (several seconds, more with tools).
    # Run it off the event loop or every other visitor's page load waits
    # behind this one chat reply.
    reply = await asyncio.to_thread(agent.process_conversation, history, message)
    copilot_memory.append_turn(db, "webchat", session_key, "user", message)
    copilot_memory.append_turn(db, "webchat", session_key, "assistant", reply)
    # Visitor transcripts carry home addresses and phone numbers; enforce the
    # retention window on the write path so it holds without a restart.
    copilot_memory.purge_expired(db)
    return _with_webchat_cookie(
        JSONResponse(content={"reply": reply}), session_key
    )


@app.get("/api/chat/history")
async def web_chat_history(request: Request, response: Response, db=Depends(get_db)):
    session_key, _ = _webchat_session(request)
    return _with_webchat_cookie(
        JSONResponse(content={
            "messages": copilot_memory.load_history(db, "webchat", session_key, limit=200)
        }),
        session_key,
    )


@app.post("/api/chat/reset")
async def web_chat_reset(request: Request, response: Response, db=Depends(get_db)):
    session_key, _ = _webchat_session(request)
    return _with_webchat_cookie(
        JSONResponse(content={
            "ok": True,
            "cleared": copilot_memory.clear_history(db, "webchat", session_key),
        }),
        session_key,
    )


# --- Permit radar (Shovels.ai) ---------------------------------------------
_permit_status_data = {}
_permit_status_lock = threading.Lock()
_permit_backfilled = False


def _permit_status() -> dict:
    with _permit_status_lock:
        return dict(_permit_status_data)


def ingest_permits(conn, permits):
    """Upsert permits into job_leads and auto-create 'permit finder' leads for
    homeowner-driven jobs. Returns (added_jobs, added_leads)."""
    added = 0
    created_leads = []
    with conn.cursor() as cur:
        for p in permits:
            cur.execute(
                "SELECT id FROM job_leads WHERE permit_number = %s AND address = %s;",
                (p["permit_number"], p["property_address"]),
            )
            if cur.fetchone():
                continue
            try:
                est_val = float(p.get("estimated_value") or 0)
            except (TypeError, ValueError):
                est_val = 0
            cur.execute(
                "INSERT INTO job_leads (permit_number, address, city, state, work_type, job_description, "
                "contractor_name, issue_date, estimated_value, source, is_demo, property_type, postal_code, use_class) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'permits',%s,%s,%s,%s) RETURNING id;",
                (p["permit_number"], p["property_address"], p["city"], p["state"], p["work_type"],
                 p["job_description"], p["contractor_name"], p["issue_date"], est_val,
                 p.get("_demo", False), (p.get("property_type") or "")[:120],
                 (p.get("postal_code") or "")[:10],
                 permit_service.classify_use(p["work_type"], p["job_description"], p.get("property_type"))),
            )
            cur.fetchone()
            added += 1
            if p.get("_demo") or not permit_service.wants_lead(p):
                continue
            # One door, one lead. The same house routinely pulls several
            # permits (a remodel plus electrical plus plumbing), and each one
            # used to become its own lead with a different placeholder email,
            # so dedupe never matched. 1,286 permit leads covered 1,036 doors;
            # the work queue showed the same address three times and the
            # mailing list paid for the same letter twice.
            addr_norm = re.sub(r"[^a-z0-9 ]", "", (p.get("property_address") or "").lower()).strip()
            if addr_norm:
                cur.execute(
                    "SELECT id FROM leads WHERE company = 'construction' "
                    "AND source = 'permit_finder' "
                    "AND regexp_replace(lower(COALESCE(address, '')), '[^a-z0-9 ]', '', 'g') = %s "
                    "LIMIT 1;",
                    (addr_norm,),
                )
                if cur.fetchone():
                    continue
            raw = p["permit_number"] or p.get("_id") or p["property_address"]
            digest = hashlib.sha256(raw.encode()).hexdigest()
            listing = f"https://api.shovels.ai/v2/permits?id={urllib.parse.quote(p.get('_id') or '')}" if p.get("_id") else (p["permit_number"] or "")
            meta = json.dumps({
                "address": p["property_address"], "city": p["city"], "permit_number": p["permit_number"],
                "permit_id": p.get("_id", ""), "work_type": p["work_type"],
                "contractor_name": p["contractor_name"], "value": est_val, "source": "permits",
            })
            res = lead_intake.intake_lead(
                conn, company="construction", source="permit_finder",
                name=f"Permit · {p['property_address'] or p['city'] or 'Hampton Roads'}",
                # leads.phone is NOT NULL, but no permit source publishes a
                # homeowner phone number. These leads are address-based and
                # worked as canvass/direct-mail targets, not call targets.
                phone=p.get("phone") or "",
                # Placeholder mailbox, not a reachable address: is_reachable()
                # treats @lead.local as "no contact yet", so these fold into the
                # "needs contact info" bucket of the one work queue.
                email=f"con-permit-{digest[:15]}@lead.local",
                # Was NULL on all 367 rows before: the address was written only
                # into analysis_json, so mail_letters.py (which reads the
                # address column) could never see these leads. 337 doors
                # were invisible to the mail pool for this reason alone.
                address=(p["property_address"] or "").strip(),
                source_ref=f"{p['permit_number']}:{p['property_address']}",
                extra={
                    "listing_url": listing,
                    "referral_code": p["permit_number"],
                    "campaign": "permit-radar",
                    "analysis_json": meta,
                    # The permit's declared job value is the only real dollar
                    # figure attached to a permit lead. Carrying it on the lead
                    # is what lets the pipeline show a value instead of "$0"
                    # for the entire permit supply.
                    **({"estimate_low_cents": int(est_val * 100),
                        "estimate_high_cents": int(est_val * 100)} if est_val > 0 else {}),
                },
            )
            lead_id = res["lead_id"]
            created_leads.append({
                "lead_id": lead_id,
                "name": p["contractor_name"] or "Homeowner",
                "service": p["work_type"] or (p["job_description"] or "")[:80],
                "address": p["property_address"],
                "budget": f"${est_val:,.0f}" if est_val else "",
            })
        conn.commit()
    # No per-permit email to the owner. This used to send one for every
    # imported permit -- 7 cities x 30 permits a day -- which is how the owner's
    # inbox filled with hundreds of "a new building permit signals a homeowner"
    # messages while nothing reached the homeowners themselves. Imported leads
    # show on the Today screen; the enrichment loop works them and the bot
    # sends the first touch. The owner hears about a permit lead when it replies.
    if created_leads:
        print(f"[permit-scan] {len(created_leads)} new permit lead(s) queued for enrichment", flush=True)
    return added, len(created_leads)


def run_permit_import():
    # is_configured() only reports a Shovels key, but the municipal open-data
    # feeds need no key. Gating on it here meant the whole importer was skipped
    # in production, where Shovels has never been configured, so /leads?lane=permits
    # only ever held demo rows.
    cities = [c.strip() for c in (os.getenv("PERMIT_CITIES", "Virginia Beach,Norfolk") or "").split(",")]
    cities = [c for c in cities if c] or ["Virginia Beach", "Norfolk"]
    # OPEN_DATA_SOURCES is keyed lowercase; normalise so PERMIT_CITIES="Virginia Beach"
    # resolves. Check truthiness of the fetcher list, not key presence: a city
    # with no feed is present but empty on purpose.
    have_open = any(permit_service.OPEN_DATA_SOURCES.get(c.strip().lower()) for c in cities)
    if not permit_service.is_configured() and not have_open:
        print("[permit-scan] no permit source configured (set SHOVELS_API_KEY or add an open-data city)", flush=True)
        return
    global _permit_backfilled
    started = datetime.utcnow()
    err = ""
    added = leads = 0
    try:
        try:
            backfill = int(os.getenv("PERMIT_BACKFILL_DAYS", "21") or 21)
        except (TypeError, ValueError):
            backfill = 21
        try:
            incremental = int(os.getenv("PERMIT_INCREMENTAL_DAYS", "2") or 2)
        except (TypeError, ValueError):
            incremental = 2
        days = backfill if not _permit_backfilled else incremental
        permits = []
        for city in cities:
            # fetch_permits prefers Shovels when configured, else the city's
            # open-data feed, and only then seeds demo rows. Demo rows carry
            # _demo and ingest_permits() skips them for lead promotion.
            permits.extend(permit_service.fetch_permits(city, "VA", days=days, limit=200))
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            added, leads = ingest_permits(conn, permits)
            # Rows ingested before property_type was persisted still have no use
            # classification. Fill them in so the owner can filter commercial
            # work out of the list instead of knocking doors at businesses.
            permit_service.backfill_classification(conn)
        _permit_backfilled = True
    except Exception as exc:
        err = str(exc)[:300]
        print(f"[permit-scan] pass failed: {exc}", flush=True)
    with _permit_status_lock:
        _permit_status_data.update({
            "last_run": started.isoformat(), "added": added, "leads": leads,
            "error": err, "running": False,
        })
    print(f"[permit-scan] added={added} job_leads, auto_leads={leads}, {err or 'ok'}", flush=True)


def _start_permit_importer():
    def _runner():
        print("[permit-scan] scheduler started", flush=True)
        while True:
            try:
                run_permit_import()
            except Exception as exc:
                print(f"[permit-scan] loop error: {exc}", flush=True)
            try:
                hours = int(os.getenv("PERMIT_SCAN_HOURS", "24") or 24)
            except (TypeError, ValueError):
                hours = 24
            time.sleep(max(hours, 1) * 3600)
    threading.Thread(target=_runner, daemon=True).start()


# --- Job finder (building permits) -----------------------------------------
@app.get("/job-leads", response_class=HTMLResponse)
async def job_leads_page(request: Request, status_filter: str = "", db=Depends(get_db)):
    """Permits now live in the intel lane of /leads. Keep old links working."""
    return RedirectResponse(url="/leads?lane=permits", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/permits", response_class=HTMLResponse)
async def permits_page(request: Request, status_filter: str = "", db=Depends(get_db)):
    return RedirectResponse(url="/leads?lane=permits", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/api/permits/stats")
async def permits_stats_api(request: Request, db=Depends(get_db)):
    """Live permit tallies, recomputed from job_leads on every request.

    Polled by the /leads page so the numbers track whatever changed them --
    a card dragged on the board, the Copilot, the voice agent, a permit
    refresh, or a direct SQL edit. There is no cache to invalidate and no
    counter to drift, because nothing is stored: every hit is a fresh
    aggregate over the table.

    Auth-gated: these tallies expose how much unworked work exists.
    """
    require_admin(request)
    with db.cursor() as cur:
        stats = permit_stats.permit_stats(cur)
    return JSONResponse(content={
        **stats,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })


@app.post("/api/job-leads/refresh")
async def job_leads_refresh(request: Request, city: str = Form("Chesapeake"), state: str = Form("VA"), db=Depends(get_db)):
    require_admin(request)
    permits = permit_service.fetch_permits(city, state)
    added, leads = ingest_permits(db, permits)
    return RedirectResponse(url=f"/leads?lane=permits&ok={added} permits + {leads} promoted", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/job-leads/{job_id}/status")
async def job_lead_status(
    job_id: int,
    request: Request,
    permit_status: str = Form(...),
    db=Depends(get_db),
):
    require_admin(request)
    if permit_status not in PERMIT_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown permit status")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM job_leads WHERE id = %s;", (job_id,))
        if not cur.fetchone():
            raise HTTPException(status_code=404, detail="Job lead not found")
        cur.execute("UPDATE job_leads SET status = %s WHERE id = %s;", (permit_status, job_id))
        db.commit()
    back = request.headers.get("referer") or "/leads?lane=permits"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/job-leads/{job_id}/reach")
async def job_lead_reach(
    job_id: int,
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    db=Depends(get_db),
):
    """You worked a permit and got a real contact. Promote it into the pipeline."""
    require_admin(request)
    phone = (phone or "").strip()
    email = (email or "").strip()
    if not phone and not email:
        raise HTTPException(status_code=400, detail="Need a phone or an email to promote a permit")
    with db.cursor() as cur:
        cur.execute("SELECT * FROM job_leads WHERE id = %s;", (job_id,))
        job = cur.fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job lead not found")
    contact_name = (name or "").strip() or (job.get("contractor_name") or "") or "Property owner"
    try:
        est_val = float(job.get("estimated_value") or 0)
    except (TypeError, ValueError):
        est_val = 0
    res = lead_intake.intake_lead(
        db, company="construction", source="permit_finder", name=contact_name,
        phone=phone or "", email=email or "",
        project_type=job.get("work_type") or "Renovation",
        address=job.get("address") or "",
        description=f"Permit #{job.get('permit_number') or ''}: {job.get('job_description') or ''} (est. value ${est_val:,.0f})",
        source_ref=f"{job.get('permit_number')}:{job.get('address')}",
    )
    lead_id = res["lead_id"]
    with db.cursor() as cur:
        cur.execute("UPDATE job_leads SET status = 'contacted' WHERE id = %s;", (job_id,))
        db.commit()
    return RedirectResponse(
        url=f"/leads?status=contacted&q={urllib.parse.quote(contact_name)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/api/job-leads/{job_id}/convert")
async def job_lead_convert(job_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM job_leads WHERE id = %s;", (job_id,))
        job = cur.fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job lead not found")
    res = lead_intake.intake_lead(
        db, company="construction", source="permit_finder",
        name=job["contractor_name"] or job["city"] or "Property owner",
        phone=job["owner_contact"] or "", email=job["owner_contact"] or "",
        project_type=job["work_type"] or "Renovation",
        address=job["address"] or "",
        description=f"Permit #{job['permit_number'] or ''}: {job['job_description'] or ''} (est. value ${job['estimated_value'] or 0:,.0f})",
        source_ref=f"{job.get('permit_number')}:{job.get('address')}",
    )
    lead_id = res["lead_id"]
    with db.cursor() as cur:
        cur.execute("UPDATE job_leads SET status = 'contacted' WHERE id = %s;", (job_id,))
        db.commit()
    try:
        auto_reply.auto_reply_to_lead(
            db, "construction", name=job["contractor_name"] or job["city"] or "Property owner",
            phone=job["owner_contact"] or "", email=job["owner_contact"] or "",
            service=job["work_type"] or "project", address=job["address"] or "",
            message=f"Permit #{job['permit_number'] or ''}: {job['job_description'] or ''}",
            source="permit_finder", lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ job-lead auto-reply failed: {e}", flush=True)
    try:
        _follow_up_new_lead(lead_id)
    except Exception as e:
        print(f"⚠️ job-lead follow-up failed: {e}", flush=True)
    return RedirectResponse(url="/job-leads?ok=converted+to+lead+%23" + str(lead_id), status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/job-leads/{job_id}/dismiss")
async def job_lead_dismiss(job_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE job_leads SET status = 'closed' WHERE id = %s;", (job_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/job-leads", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/job-leads/{job_id}/delete")
async def job_lead_delete(job_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM job_leads WHERE id = %s;", (job_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/job-leads", status_code=status.HTTP_303_SEE_OTHER)


# --- Bid opportunities (solicitations we are chasing) ----------------------
# Separate from `leads` on purpose. Creating a `leads` row fires
# auto_reply.auto_reply_to_lead and the outbound voice agent, and a general
# contractor is not a customer. Nothing below sends email or dials.
_BID_COMPANY = "construction"


@app.get("/bids/{bid_id}")
async def bid_detail_page(bid_id: int, request: Request, db=Depends(get_db)):
    """One bid, with the paperwork that belongs to it.

    The board used to be the end of the road: every row was a div with no href,
    so there was nowhere to click into and invoices and cost lines -- which have
    had working endpoints all along -- could not be reached from a bid at all.
    This is that page.
    """
    require_admin(request)
    bid = bid_opportunities.get_bid(db, bid_id, _BID_COMPANY)
    if not bid:
        return RedirectResponse(url="/bids?missing=1", status_code=status.HTTP_303_SEE_OTHER)

    costs = []
    summary = {}
    invoices: list = []
    inv_summary: dict = {}
    docs: list = []
    try:
        bid_costing.ensure_accounting_schema(db)
        costs = bid_costing.cost_lines(db, bid_id, _BID_COMPANY)
        summary = bid_costing.cost_summary(db, bid_id, _BID_COMPANY)
        invoices = bid_costing.list_invoices(db, bid_id, _BID_COMPANY)
        inv_summary = bid_costing.invoice_summary(db, bid_id, _BID_COMPANY)
    except Exception as exc:
        # A missing accounting table must not cost the owner the whole page.
        print(f"[bids] accounting unavailable for bid {bid_id}: {exc}", flush=True)
    try:
        bid_opportunities.ensure_schema(db)
        docs = bid_opportunities.list_documents(db, bid_id, _BID_COMPANY)
    except Exception as exc:
        print(f"[bids] documents unavailable for bid {bid_id}: {exc}", flush=True)

    return templates.TemplateResponse(
        request=request, name="bid_detail.html",
        context={
            "b": bid, "costs": costs, "cost_summary": summary,
            "invoices": invoices, "inv_summary": inv_summary,
            "docs": docs,
            "statuses": ["new", "drafting", "sent", "shortlist",
                         "won", "lost", "withdrawn"],
            "cost_categories": ["material", "labor", "sub", "equipment", "other"],
        })


@app.get("/api/bids")
async def bids_api(request: Request, status: str = "", db=Depends(get_db)):
    require_admin(request)
    return JSONResponse(content=bid_opportunities.list_bids(db, _BID_COMPANY, status))


@app.get("/api/bids/stats")
async def bids_stats_api(request: Request, db=Depends(get_db)):
    """Auth-gated: exposes unworked contract value."""
    require_admin(request)
    return JSONResponse(content=bid_opportunities.stats(db, _BID_COMPANY))


@app.post("/api/bids")
async def bid_create(
    request: Request,
    name: str = Form(...),
    trade: str = Form(""),
    owner: str = Form(""),
    reference: str = Form(""),
    role: str = Form("sub"),
    due_date: str = Form(""),
    bid_amount: str = Form(""),
    location: str = Form(""),
    notes: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    if role not in bid_opportunities.ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {bid_opportunities.ROLES}")
    fields = dict(name=name.strip(), trade=trade.strip(), owner=owner.strip(),
                  reference=reference.strip(), role=role, location=location.strip(),
                  notes=notes.strip())
    if due_date.strip():
        try:
            fields["due_date"] = date.fromisoformat(due_date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="due_date must be YYYY-MM-DD")
    if bid_amount.strip():
        try:
            fields["bid_amount"] = float(bid_amount.replace("$", "").replace(",", "").strip())
        except ValueError:
            raise HTTPException(status_code=400, detail="bid_amount must be a number")
    try:
        bid_id = bid_opportunities.create_bid(db, _BID_COMPANY, **fields)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/bids?added=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/status")
async def bid_set_status(bid_id: int, request: Request,
                         new_status: str = Form(..., alias="status"),
                         db=Depends(get_db)):
    # The parameter is aliased rather than renamed: the form field the bid cards
    # post is still "status", but a parameter literally named `status` shadowed
    # the fastapi module, so `status.HTTP_303_SEE_OTHER` raised
    # AttributeError and the redirect 500'd after the status had already been
    # written.
    require_admin(request)
    try:
        if not bid_opportunities.set_status(db, bid_id, new_status, _BID_COMPANY):
            raise HTTPException(status_code=404, detail="Bid not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Won/lost is an accounting event, so the outcome columns are written
    # here rather than in set_status. No revenue is recognised on a win --
    # it is recognised when the job is billed, per ledger_sync.sync_bid_outcome.
    if new_status in ("won", "lost", "withdrawn"):
        try:
            bid_costing.set_outcome(db, bid_id, new_status, _BID_COMPANY)
        except Exception as e:
            logger.warning("outcome columns not written for bid %s: %s", bid_id, e)
    back = request.headers.get("referer") or "/bids"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/amount")
async def bid_set_amount(bid_id: int, request: Request, amount: str = Form(...),
                         db=Depends(get_db)):
    require_admin(request)
    try:
        val = float(amount.replace("$", "").replace(",", "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="amount must be a number")
    if not bid_opportunities.set_bid_amount(db, bid_id, val, _BID_COMPANY):
        raise HTTPException(status_code=404, detail="Bid not found")
    back = request.headers.get("referer") or "/bids"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/note")
async def bid_add_note(bid_id: int, request: Request, note: str = Form(...),
                       db=Depends(get_db)):
    require_admin(request)
    if not note.strip():
        raise HTTPException(status_code=400, detail="note is empty")
    if not bid_opportunities.add_note(db, bid_id, note.strip(), _BID_COMPANY):
        raise HTTPException(status_code=404, detail="Bid not found")
    back = request.headers.get("referer") or "/bids"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/delete")
async def bid_delete(bid_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    if not bid_opportunities.delete_bid(db, bid_id, _BID_COMPANY):
        raise HTTPException(status_code=404, detail="Bid not found")
    return RedirectResponse(url="/bids?deleted=1", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/{bid_id}/documents")
async def bid_document_upload(bid_id: int, request: Request, doc: UploadFile = Form(...),
                              db=Depends(get_db)):
    require_admin(request)
    bid_opportunities.ensure_schema(db)
    bid = bid_opportunities.get_bid(db, bid_id, _BID_COMPANY)
    if not bid:
        raise HTTPException(status_code=404, detail="Bid not found")
    raw = await doc.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    ext = os.path.splitext(doc.filename or "")[1][:10] or ".pdf"
    fname = f"bid-{bid_id}-{uuid.uuid4().hex[:12]}{ext.lower()}"
    try:
        stored = photo_store.save(raw, fname, key_prefix=f"bids/{bid_id}")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    bid_opportunities.add_document(db, bid_id, _BID_COMPANY,
                                   filename=(doc.filename or fname)[:500],
                                   key=stored["key"], url=stored["url"],
                                   content_type=doc.content_type,
                                   bytes_count=len(raw),
                                   uploaded_by=_owner_email())
    back = request.headers.get("referer") or f"/bids/{bid_id}"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/bids/documents/{doc_id}/delete")
async def bid_document_delete(doc_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    bid_opportunities.ensure_schema(db)
    bid_opportunities.delete_document(db, doc_id, _BID_COMPANY)
    back = request.headers.get("referer") or "/bids"
    return RedirectResponse(url=back, status_code=status.HTTP_303_SEE_OTHER)


@app.get("/api/bids/sources")
async def bid_sources_api(request: Request, db=Depends(get_db)):
    """Which external bid sources are wired and which env vars each needs.

    Admin only, and it reports configuration state rather than values -- never
    echoes a token back out.
    """
    require_admin(request)
    return JSONResponse(content={
        "sources": bid_sources.source_status(),
        "recent": bid_opportunities.recent_pulls(db, _BID_COMPANY),
    })


@app.post("/api/bids/pull")
async def bid_pull(request: Request, source: str = Form("json_feed"), db=Depends(get_db)):
    """Pull solicitations from an external source into bid_opportunities.

    Admin only. This is the button on /bids. Everything is logged to
    bid_pull_runs whether it succeeds or fails, so a failed pull shows you why
    instead of silently leaving the board stale.
    """
    require_admin(request)
    run_id = bid_opportunities.start_pull(db, source, _BID_COMPANY)
    try:
        rows = bid_sources.pull(source, user_email=_owner_email())
    except Exception as e:
        bid_opportunities.finish_pull(db, run_id, ok=False, error=str(e))
        return JSONResponse(content={"ok": False, "source": source,
                                     "detail": str(e)}, status_code=502)
    result = bid_opportunities.upsert_pulled(db, rows, _BID_COMPANY)
    detail = None
    if result["errors"]:
        detail = f"{len(result['errors'])} row(s) rejected; see the log."
    bid_opportunities.finish_pull(
        db, run_id, fetched=len(rows), created=result["created"],
        updated=result["updated"], ok=True, error=detail)
    return JSONResponse(content={"ok": True, "source": source, **result})


@app.post("/api/bids/ingest")
async def bid_ingest(request: Request, text: str = Form(""), db=Depends(get_db)):
    """Turn pasted text into board rows. Admin only.

    The zero-configuration path, so the button does something without any
    external API wired up. Accepts a CSV/TSV export from a bid system, or the
    text of a solicitation email. Blank lines separate records.

    Writes through upsert_pulled, so it cannot overwrite a status and
    cannot duplicate one it already holds.
    """
    require_admin(request)
    if not text or not text.strip():
        return JSONResponse(content={"ok": False, "detail": "Nothing pasted."}, status_code=400)
    if len(text) > 400_000:
        return JSONResponse(content={"ok": False, "detail": "That is too large to paste."},
                            status_code=400)
    try:
        rows, note = bid_sources.parse_pasted(text)
    except Exception as e:
        return JSONResponse(content={"ok": False, "detail": f"Could not read that: {e}"},
                            status_code=400)
    if not rows:
        return JSONResponse(content={"ok": False, "detail": note or "Nothing readable in that."},
                            status_code=400)
    run_id = bid_opportunities.start_pull(db, "paste", _BID_COMPANY)
    result = bid_opportunities.upsert_pulled(db, rows, _BID_COMPANY)
    try:
        bid_opportunities.deduplicate_bids(db, _BID_COMPANY)
    except Exception as e:
        print(f"⚠️ bid dedup after ingest failed: {e}", flush=True)
    detail = None if not result["errors"] else f"{len(result['errors'])} rejected."
    bid_opportunities.finish_pull(db, run_id, fetched=len(rows), created=result["created"],
                                  updated=result["updated"], ok=True, error=detail)
    return JSONResponse(content={"ok": True, "note": note, **result})


@app.post("/api/bids/deduplicate")
async def bids_deduplicate(request: Request, db=Depends(get_db)):
    require_admin(request)
    try:
        bid_opportunities.ensure_schema(db)
        res = bid_opportunities.deduplicate_bids(db, _BID_COMPANY)
    except Exception as e:
        return JSONResponse(content={"ok": False, "error": str(e)}, status_code=500)
    return JSONResponse(content={"ok": True, **res})


@app.get("/bids", response_class=HTMLResponse)
async def bids_page(request: Request, status: str = "", db=Depends(get_db)):
    require_admin(request)
    rows = bid_opportunities.list_bids(db, _BID_COMPANY, status)
    pulls = bid_opportunities.recent_pulls(db, _BID_COMPANY, 6)
    # "New" means first seen in the most recent pull. No pulls yet means nothing
    # is new, which is right: a hand-entered bid did not arrive from a source.
    # "New" means first seen in the most recent pull. Default to the epoch
    # rather than None: pulls_since is compared against first_seen_at in the
    # template, and a None there raises TypeError and 500s the whole board the
    # moment a bid exists but no successful pull has run yet -- which is exactly
    # the state a webhook-only setup starts in.
    pulls_since = next((p["started_at"] for p in pulls if p.get("ok")),
                       datetime(1970, 1, 1, tzinfo=timezone.utc))
    return templates.TemplateResponse(
        request=request, name="bids.html",
        context={
            "rows": rows,
            "statuses": bid_opportunities.STATUSES,
            "roles": bid_opportunities.ROLES,
            "sel": status,
            "today": date.today(),
            "stats": bid_opportunities.stats(db, _BID_COMPANY),
            "sources": bid_sources.source_status(),
            "pulls": pulls,
            "pulls_since": pulls_since,
            # BuildingConnected is the source that matters: configured means the
            # APS app vars exist; connected means the owner has completed the
            # Autodesk sign-in and a token is stored for them.
            "bc_configured": autodesk_oauth.configured(),
            "bc_connected": bool(autodesk_oauth.get_tokens(_owner_email())) if autodesk_oauth.configured() else False,
        },
    )


# --- Website lead webhook (for external sites/forms) -------------------------
@app.post("/api/webhooks/leads")
async def website_lead_webhook(request: Request, db=Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    name = data.get("name") or data.get("full_name") or ""
    phone = data.get("phone") or ""
    email = data.get("email") or ""
    project_type = data.get("project_type") or data.get("service") or ""
    address = data.get("address") or ""
    description = data.get("description") or data.get("message") or data.get("notes") or ""
    source = data.get("source") or "website_webhook"
    
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    
    try:
        res = lead_intake.intake_lead(
            db, company="construction", source=source, name=name, phone=phone,
            email=(email or "").strip(), address=address, description=description,
            project_type=project_type,
            extra=data,
        )
        lead_id = res["lead_id"]
        created = res.get("created", True)
        
        if created:
            try:
                auto_reply.notify_owner_email(
                    "construction", lead_id=lead_id, name=name, phone=phone,
                    email=(email or "").strip(), service=(project_type or ""), address=address,
                    budget=data.get("budget", ""), timeline=data.get("timeline", ""),
                    message=description, source=source,
                )
            except Exception as e:
                print(f"⚠️ webhook lead owner notify failed: {e}", flush=True)
            try:
                auto_reply.auto_reply_to_lead(
                    db, "construction", name=name, phone=phone, email=(email or "").strip(),
                    service=(project_type or ""), address=address,
                    budget=data.get("budget", ""), timeline=data.get("timeline", ""),
                    message=description, source=source, lead_id=lead_id,
                )
            except Exception as e:
                print(f"⚠️ webhook lead auto-reply failed: {e}", flush=True)
            try:
                _follow_up_new_lead(lead_id)
            except Exception as e:
                print(f"⚠️ webhook lead follow-up failed: {e}", flush=True)
            try:
                await asyncio.to_thread(lead_pipeline.on_lead_created, db, lead_id)
            except Exception as e:
                print(f"⚠️ webhook lead cascade failed: {e}", flush=True)
            await asyncio.to_thread(zoho_sync.after_intake, db, lead_id)
        
        return JSONResponse(content={"status": "success", "lead_id": lead_id, "created": created})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Zoho CRM webhook (for updates from Zoho) ---------------------------------
@app.post("/api/webhooks/zoho")
async def zoho_webhook(request: Request, db=Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    
    # Zoho can send lead updates in different formats
    # This is a basic receiver - can be expanded based on actual Zoho webhook format
    print(f"📥 Zoho webhook received: {json.dumps(data)[:200]}", flush=True)
    return JSONResponse(content={"status": "received"})


# --- Inbound bid webhook ----------------------------------------------------
# Auth is HMAC-SHA256 over the raw body, sent as X-Bids-Signature. Unconfigured
# means the endpoint is closed, not open -- it returns 503 rather than accepting
# unsigned posts. Nothing here writes to `leads`, so no lead outreach can fire.
@app.post("/api/webhooks/bids")
async def bids_webhook(request: Request, db=Depends(get_db)):
    if not bid_opportunities.webhook_enabled():
        raise HTTPException(status_code=503, detail="Bid webhook not configured")

    raw = (await request.body()).decode("utf-8", errors="replace")
    sig = request.headers.get("X-Bids-Signature", "")
    if not bid_opportunities.verify_signature(raw, sig):
        raise HTTPException(status_code=401, detail="Bad signature")

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Body must be valid JSON")

    # A single post may carry one object or a batch under "bids".
    items = payload.get("bids") if isinstance(payload, dict) and isinstance(payload.get("bids"), list) else [payload]

    results, rejected, errors = [], [], []
    for item in items[:100]:
        try:
            r = bid_opportunities.upsert_from_webhook(db, item, _BID_COMPANY)
        except Exception as e:
            errors.append(str(e))
            continue
        # upsert reports validation failures as a normal return, so they have to be
        # split out here -- otherwise a payload with no name counts as a success and
        # the sender gets a 200 for something that was thrown away.
        if r.get("error"):
            rejected.append({"error": r["error"], "name": (item or {}).get("name")})
        else:
            results.append(r)

    created = sum(1 for r in results if r.get("created"))
    updated = len(results) - created
    print(f"🪝 bid webhook: {created} new, {updated} updated, "
          f"{len(rejected)} rejected, {len(errors)} errors", flush=True)

    # 200 only when at least one row actually landed.
    status = 200 if results else (422 if rejected or errors else 400)
    return JSONResponse(
        content={
            "received": len(items),
            "created": created,
            "updated": updated,
            "results": results,
            "rejected": rejected,
            "errors": errors,
        },
        status_code=status,
    )


# Helper the sender uses to sign. Not mounted -- run it locally against the secret.
@app.get("/api/webhooks/bids/health")
async def bids_webhook_health(request: Request):
    return JSONResponse(content={
        "configured": bid_opportunities.webhook_enabled(),
        "endpoint": "/api/webhooks/bids",
        "signature_header": "X-Bids-Signature",
        "algorithm": "HMAC-SHA256 over the raw request body, hex digest",
        "accepts": sorted(bid_opportunities._WEBHOOK_FIELDS),
        "notes": "Idempotent on (reference, trade). status is not overwritten on update.",
    })


# --- Projects (active jobs for the crew) ------------------------------------
PROJECT_STATUSES = ["planned", "active", "on_hold", "completed", "cancelled"]


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, status_filter: str = "", lead_id: int = 0,
                        project_id: int = 0,
                        db=Depends(get_db)):
    require_admin(request)
    where, params = [], []
    if status_filter:
        where.append("p.status = %s")
        params.append(status_filter)
    if project_id:
        where.append("p.id = %s")
        params.append(project_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(f"""
            SELECT p.*, l.name AS lead_name,
                   COALESCE((SELECT string_agg(c.name, ', ' ORDER BY c.name)
                             FROM project_assignments pa JOIN crew c ON c.id = pa.crew_id
                             WHERE pa.project_id = p.id), '') AS crew_names,
                   (SELECT COUNT(*) FROM project_assignments pa2 WHERE pa2.project_id = p.id) AS crew_count,
                   (SELECT COUNT(*) FROM job_photos ph WHERE ph.project_id = p.id) AS photo_count
            FROM projects p LEFT JOIN leads l ON l.id = p.lead_id
            {clause} ORDER BY p.start_date NULLS LAST, p.created_at DESC LIMIT 300;
        """, tuple(params))
        projects = cur.fetchall()
        cur.execute("SELECT id, name FROM crew ORDER BY name;")
        crew_rows = cur.fetchall()
        cur.execute("SELECT id, name, phone, address, project_type, description, status FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 25;")
        lead_rows = cur.fetchall()
        selected_lead = next((row for row in lead_rows if row["id"] == lead_id), None)
        if lead_id and not selected_lead:
            cur.execute(
                "SELECT id, name, phone, address, project_type, description, status FROM leads "
                "WHERE id = %s AND company = 'construction';",
                (lead_id,),
            )
            selected_lead = cur.fetchone()
            if selected_lead:
                lead_rows.insert(0, selected_lead)
    return templates.TemplateResponse(request=request, name="projects.html", context={
        "user": {"email": require_auth(request)[1]},
        "projects": projects, "crew": crew_rows, "leads": lead_rows,
        "selected_lead": selected_lead,
        "statuses": PROJECT_STATUSES, "active_status": status_filter,
    })


@app.post("/api/projects")
async def project_create(request: Request, name: str = Form(""), lead_id: str = Form(""), address: str = Form(""),
                         summary: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    lead_pk = None
    with db.cursor() as cur:
        if lead_id:
            try:
                lead_pk = int(lead_id)
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid lead ID")
            cur.execute(
                "SELECT name, address, project_type, description FROM leads "
                "WHERE id = %s AND company = 'construction';",
                (lead_pk,),
            )
            lead = cur.fetchone()
            if not lead:
                raise HTTPException(status_code=404, detail="Construction lead not found")
            name = name or f"{lead['project_type'] or 'Project'} — {lead['name']}"
            address = address or lead["address"] or ""
            summary = summary or lead["description"] or ""
        cur.execute(
            "INSERT INTO projects (lead_id, name, address, summary, status) VALUES (%s,%s,%s,%s,'planned') RETURNING id;",
            (lead_pk, name or "Untitled project", address, summary),
        )
        pid = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(
        url=f"/projects?project_id={pid}#project-{pid}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@app.post("/api/projects/{project_id}/update")
async def project_update(request: Request, project_id: int, db=Depends(get_db),
                         name: str = Form(""), address: str = Form(""), summary: str = Form(""),
                         status_val: str = Form("planned"), start_date: str = Form(""),
                         end_date: str = Form(""), notes: str = Form("")):
    require_admin(request)
    if status_val not in PROJECT_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status")
    with db.cursor() as cur:
        cur.execute(
            "UPDATE projects SET name=%s, address=%s, summary=%s, status=%s, "
            "start_date=%s, end_date=%s, notes=%s WHERE id=%s RETURNING lead_id;",
            (name, address, summary, status_val, start_date or None, end_date or None, notes, project_id),
        )
        project = cur.fetchone()
        if project and project.get("lead_id") and status_val in ("active", "completed"):
            lead_status = "in_progress" if status_val == "active" else "completed"
            cur.execute(
                "UPDATE leads SET status = %s WHERE id = %s AND company = 'construction';",
                (lead_status, project["lead_id"]),
            )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/projects", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/projects/{project_id}/assign")
async def project_assign(request: Request, project_id: int, crew_id: int = Form(...), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO project_assignments (project_id, crew_id) VALUES (%s,%s) ON CONFLICT DO NOTHING;",
            (project_id, crew_id),
        )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/projects", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/projects/{project_id}/unassign")
async def project_unassign(request: Request, project_id: int, crew_id: int = Form(...), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM project_assignments WHERE project_id=%s AND crew_id=%s;", (project_id, crew_id))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/projects", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/projects/{project_id}/delete")
async def project_delete(project_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM projects WHERE id=%s;", (project_id,))
        db.commit()
    return RedirectResponse(url="/projects?ok=deleted", status_code=status.HTTP_303_SEE_OTHER)


# --- Back-log ---------------------------------------------------------------
# Record work that already happened (before the app existed) with its real
# date, so the lead pipeline and payment history reflect reality.
BACKLOG_PROJECT_STATUS = {"deposit": "planned", "in_progress": "active", "completed": "completed"}


def _backlog_when(value: str) -> datetime:
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        d = date.today()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=APP_TZ)


def _backlog_cents(value: str) -> int:
    try:
        return max(0, int(round(float(value) * 100)))
    except (TypeError, ValueError):
        return 0


@app.get("/backlog", response_class=HTMLResponse)
async def backlog_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "SELECT id, name, address, project_type, status, estimate_low_cents, "
            "estimate_high_cents, created_at FROM leads WHERE source = 'backlog' AND company = 'construction' "
            "ORDER BY created_at DESC LIMIT 100;"
        )
        entries = cur.fetchall()
        cur.execute(
            "SELECT COUNT(*) AS jobs, COALESCE(SUM(estimate_high_cents), 0) AS pipeline "
            "FROM leads WHERE source = 'backlog' AND company = 'construction';"
        )
        totals = cur.fetchone()
        cur.execute(
            "SELECT COALESCE(SUM(p.amount_cents), 0) AS collected FROM payments p "
            "JOIN leads l ON l.id = p.lead_id WHERE l.source = 'backlog' AND l.company = 'construction';"
        )
        collected = cur.fetchone()["collected"]
    return templates.TemplateResponse(
        request=request, name="backlog.html",
        context={
            "entries": entries, "totals": totals, "collected": collected,
            "statuses": LEAD_STATUSES, "today": date.today().isoformat(),
            "project_types": INSTANT_PROJECT_TYPES,
        },
    )


@app.post("/api/backlog")
async def backlog_create(
    request: Request,
    entry_date: str = Form(""),
    name: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    project_type: str = Form(""),
    address: str = Form(""),
    description: str = Form(""),
    low_dollars: str = Form(""),
    high_dollars: str = Form(""),
    status_val: str = Form("completed"),
    paid_dollars: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    when = _backlog_when(entry_date)
    lead_status = status_val if status_val in LEAD_STATUSES else "completed"
    low_cents, high_cents = _backlog_cents(low_dollars), _backlog_cents(high_dollars)
    paid_cents = _backlog_cents(paid_dollars)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, phone, email, project_type, address, description, source, "
            "status, estimate_low_cents, estimate_high_cents, created_at, company) "
            "VALUES (%s,%s,%s,%s,%s,%s,'backlog',%s,%s,%s,%s,'construction') RETURNING id;",
            (name or "Back-logged job", phone or "", email, project_type, address, description,
             lead_status, low_cents or None, high_cents or None, when),
        )
        lead_id = cur.fetchone()["id"]
        project_status = BACKLOG_PROJECT_STATUS.get(lead_status)
        if project_status:
            cur.execute(
                "INSERT INTO projects (lead_id, name, address, summary, start_date, status, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s);",
                (lead_id, f"{project_type or 'Project'} — {name or 'Back-logged job'}", address,
                 description, when.date(), project_status, when),
            )
        if paid_cents > 0:
            cur.execute(
                "INSERT INTO payments (lead_id, amount_cents, status, created_at, company) "
                "VALUES (%s,%s,'paid',%s,'construction');",
                (lead_id, paid_cents, when),
            )
        db.commit()
    return RedirectResponse(url="/backlog?ok=added", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/backlog/{lead_id}/delete")
async def backlog_delete(lead_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM payments WHERE lead_id=%s;", (lead_id,))
        cur.execute("DELETE FROM projects WHERE lead_id=%s;", (lead_id,))
        cur.execute("DELETE FROM leads WHERE id=%s AND source='backlog';", (lead_id,))
        db.commit()
    return RedirectResponse(url="/backlog?ok=deleted", status_code=status.HTTP_303_SEE_OTHER)


# --- Crew admin -------------------------------------------------------------
CREW_ROLES = ["owner", "foreman", "carpenter", "framer", "laborer", "electrician", "plumber", "painter", "finisher", "grunt"]
CREW_PAY_TYPES = ["hourly", "salary", "1099"]


@app.get("/crew/admin", response_class=HTMLResponse)
async def crew_admin_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew ORDER BY is_active DESC, name;")
        crew = cur.fetchall()
    return templates.TemplateResponse(request=request, name="crew_admin.html", context={
        "user": {"email": require_auth(request)[1]},
        "crew": crew, "roles": CREW_ROLES, "pay_types": CREW_PAY_TYPES,
    })


@app.get("/crew/access", response_class=HTMLResponse)
async def crew_access_page(request: Request, db=Depends(get_db)):
    require_admin(request)
    return templates.TemplateResponse(request=request, name="crew_access.html", context={
        "user": {"email": require_auth(request)[1]},
        "saved": request.query_params.get("saved"),
        "features": _crew_features(db),
        "catalog": auth_service.CREW_FEATURES,
        "bulletin": _get_setting(db, BULLETIN_KEY, ""),
    })


@app.post("/api/crew/access")
async def crew_access_save(request: Request, db=Depends(get_db)):
    require_admin(request)
    form = await request.form()
    features = dict(auth_service.DEFAULT_CREW_FEATURES)
    for key in features:
        features[key] = (form.get(f"feature_{key}") == "on")
    _set_setting(db, CREW_PERMS_KEY, json.dumps(features))
    _set_setting(db, BULLETIN_KEY, (str(form.get("bulletin") or "")).strip()[:500])
    return RedirectResponse(url="/crew/access?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/crew/admin/training", response_class=HTMLResponse)
async def crew_training_report(request: Request, db=Depends(get_db)):
    require_admin(request)
    tests = training_service.ALL_TRAININGS
    with db.cursor() as cur:
        cur.execute("SELECT id, name, role, is_active FROM crew ORDER BY is_active DESC, name;")
        crew = cur.fetchall()
        cur.execute(
            "SELECT worker_id, test_slug, ROUND(100.0 * score / NULLIF(total, 0)) AS pct, passed, created_at "
            "FROM worker_quiz_results ORDER BY created_at DESC;"
        )
        results = cur.fetchall()
    crew_ids = [c["id"] for c in crew]
    best = {}  # (worker_id, test_slug) -> best percent
    ever_passed = {}  # (worker_id, test_slug) -> True
    for r in results:
        if r["worker_id"] not in crew_ids:
            continue
        key = (r["worker_id"], r["test_slug"] or "osha")
        best[key] = max(best.get(key, 0), r["pct"] or 0)
        if r["passed"]:
            ever_passed[key] = True
    matrix = []
    for c in crew:
        done = []
        for t in tests:
            key = (c["id"], t["slug"])
            done.append({
                "slug": t["slug"], "title": t["title"],
                "best": best.get(key),
                "passed": bool(ever_passed.get(key)),
            })
        matrix.append({"crew": {"id": c["id"], "name": c["name"], "role": c["role"], "is_active": c["is_active"]}, "tests": done})
    return templates.TemplateResponse(request=request, name="crew_training_report.html", context={
        "user": {"email": require_auth(request)[1]},
        "crew": crew, "tests": tests, "matrix": matrix,
    })


@app.post("/api/crew/training/remind")
async def crew_training_remind(request: Request, crew_id: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    tools = build_tool_handlers(db, stripe_svc)
    try:
        cid = int(crew_id or 0)
    except (TypeError, ValueError):
        cid = 0
    result = tools["remind_crew_training"](crew_id=cid)
    return JSONResponse(result)


@app.post("/api/crew")
async def crew_create(request: Request, name: str = Form(...), phone: str = Form(""), email: str = Form(""),
                      role: str = Form("laborer"), pay_type: str = Form("hourly"), pay_rate: str = Form("0"),
                      pin: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    pin_hash = None
    if pin.strip():
        if not (4 <= len(pin.strip()) <= 6 and pin.strip().isdigit()):
            raise HTTPException(status_code=400, detail="PIN must be 4-6 digits")
        pin_hash = _hash_pin(pin.strip())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO crew (name, phone, email, role, pay_type, pay_rate, pin_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id;",
            (name, phone, email, role, pay_type, pay_rate or 0, pin_hash),
        )
        pid = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url=f"/crew/admin?ok=added+%23{pid}", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/{crew_id}/update")
async def crew_update(crew_id: int, request: Request, db=Depends(get_db),
                      role: str = Form("laborer"), pay_type: str = Form("hourly"), pay_rate: str = Form("0"),
                      is_active: str = Form("")):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute(
            "UPDATE crew SET role=%s, pay_type=%s, pay_rate=%s, is_active=%s WHERE id=%s;",
            (role, pay_type, pay_rate or 0, is_active.lower() in ("on", "true", "1", "yes"), crew_id),
        )
        db.commit()
    return RedirectResponse(url="/crew/admin", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/{crew_id}/pin")
async def crew_pin(crew_id: int, request: Request, pin: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    if not (4 <= len(pin.strip()) <= 6 and pin.strip().isdigit()):
        raise HTTPException(status_code=400, detail="PIN must be 4-6 digits")
    with db.cursor() as cur:
        cur.execute("UPDATE crew SET pin_hash=%s WHERE id=%s;", (_hash_pin(pin.strip()), crew_id))
        db.commit()
    return RedirectResponse(url="/crew/admin?ok=pin+updated", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/{crew_id}/delete")
async def crew_delete(crew_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("DELETE FROM crew WHERE id=%s;", (crew_id,))
        db.commit()
    return RedirectResponse(url="/crew/admin?ok=deleted", status_code=status.HTTP_303_SEE_OTHER)


# --- Worker portal ----------------------------------------------------------
@app.get("/crew", response_class=HTMLResponse)
async def crew_portal_index(request: Request):
    if current_worker(request):
        return RedirectResponse(url="/crew/dashboard", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="crew_login.html", context={
        "error": request.query_params.get("error"),
    })


@app.post("/api/crew/login")
async def crew_login(phone: str = Form(...), pin: str = Form(...), db=Depends(get_db)):
    digits = "".join(c for c in (phone or "") if c.isdigit())
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE REGEXP_REPLACE(phone, '[^0-9]', '', 'g') = %s AND is_active = TRUE;",
                    (digits,))
        row = cur.fetchone()
    if not row or not _verify_pin((pin or "").strip(), row["pin_hash"]):
        return RedirectResponse(url="/crew?error=Invalid+phone+or+PIN", status_code=status.HTTP_303_SEE_OTHER)
    return _worker_login_response(row)


@app.get("/crew/logout")
async def crew_logout():
    resp = RedirectResponse(url="/crew", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(WORKER_COOKIE)
    return resp


@app.get("/crew/dashboard", response_class=HTMLResponse)
async def crew_dashboard(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
        cur.execute("""
            SELECT p.*, (SELECT COUNT(*) FROM job_photos ph WHERE ph.project_id = p.id) AS photo_count
            FROM project_assignments pa JOIN projects p ON p.id = pa.project_id
            WHERE pa.crew_id = %s AND p.status IN ('planned','active')
            ORDER BY p.start_date NULLS LAST, p.created_at;
        """, (worker.get("id"),))
        my_jobs = cur.fetchall()
        cur.execute("SELECT * FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL ORDER BY punch_in_at DESC LIMIT 1;",
                    (worker.get("id"),))
        open_punch = cur.fetchone()
        start = date.today() - timedelta(days=date.today().weekday())
        cur.execute("SELECT COALESCE(SUM(hours),0) AS h FROM timesheets WHERE crew_id = %s AND work_date >= %s;",
                    (worker.get("id"), start))
        week_hours = float(cur.fetchone()["h"] or 0)
        cur.execute("SELECT * FROM timesheets WHERE crew_id = %s ORDER BY work_date DESC, created_at DESC LIMIT 5;",
                    (worker.get("id"),))
        recent_sheets = cur.fetchall()
    return templates.TemplateResponse(request=request, name="crew_dashboard.html", context={
        "worker": crew_row, "jobs": my_jobs, "open_punch": open_punch,
        "week_hours": week_hours, "recent": recent_sheets,
    })


@app.get("/crew/jobs/{project_id}", response_class=HTMLResponse)
async def crew_job_detail(project_id: int, request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s;", (project_id,))
        job = cur.fetchone()
        cur.execute("""
            SELECT c.* FROM project_assignments pa JOIN crew c ON c.id = pa.crew_id
            WHERE pa.project_id = %s;
        """, (project_id,))
        crew_list = cur.fetchall()
        cur.execute("SELECT * FROM job_photos WHERE project_id = %s ORDER BY uploaded_at DESC;", (project_id,))
        photos = cur.fetchall()
        cur.execute("SELECT * FROM clock_events WHERE crew_id = %s AND project_id = %s AND punch_out_at IS NULL ORDER BY punch_in_at DESC LIMIT 1;",
                    (worker.get("id"), project_id))
        open_punch = cur.fetchone()
    return templates.TemplateResponse(request=request, name="crew_job.html", context={
        "worker": {"id": worker.get("id")}, "job": job, "crew": crew_list,
        "photos": photos, "open_punch": open_punch,
    })


@app.post("/api/crew/clock-in")
async def crew_clock_in(request: Request, project_id: int = Form(0), lat: str = Form(""), lng: str = Form(""), db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
    if not crew_row:
        raise HTTPException(status_code=401, detail="Worker not found")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL;", (crew_row["id"],))
        if cur.fetchone():
            return RedirectResponse(url=request.headers.get("referer") or "/crew", status_code=status.HTTP_303_SEE_OTHER)
        cur.execute(
            "INSERT INTO clock_events (crew_id, project_id, punch_in_at, punch_in_lat, punch_in_lng) VALUES (%s,%s,NOW(),%s,%s);",
            (crew_row["id"], project_id or None, lat or None, lng or None),
        )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/crew/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/clock-out")
async def crew_clock_out(request: Request, clock_id: int = Form(0), lat: str = Form(""), lng: str = Form(""), db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
    if not crew_row:
        raise HTTPException(status_code=401, detail="Worker not found")
    with db.cursor() as cur:
        clause = " AND id = %s" if clock_id else ""
        params = [crew_row["id"]]
        if clock_id:
            params.append(clock_id)
        cur.execute(f"SELECT * FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL{clause} ORDER BY punch_in_at DESC LIMIT 1;",
                    tuple(params))
        ev = cur.fetchone()
        if not ev:
            return RedirectResponse(url=request.headers.get("referer") or "/crew/dashboard", status_code=status.HTTP_303_SEE_OTHER)
        cur.execute("UPDATE clock_events SET punch_out_at = NOW(), punch_out_lat = %s, punch_out_lng = %s WHERE id = %s;",
                    (lat or None, lng or None, ev["id"]))
        db.commit()
        in_dt = ev["punch_in_at"]
        if in_dt:
            out_dt = datetime.now(APP_TZ)
            hours = max(round((out_dt - in_dt).total_seconds() / 3600, 2), 0)
            cur.execute(
                "INSERT INTO timesheets (crew_id, project_id, work_date, start_time, end_time, hours, work_type, source, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'regular','clock','submitted');",
                (crew_row["id"], ev["project_id"], in_dt.date(), in_dt.strftime("%H:%M"),
                 out_dt.strftime("%H:%M"), hours),
            )
            db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/crew/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/hours")
async def crew_add_hours(request: Request, project_id: int = Form(0), work_date: str = Form(""), hours: str = Form(""),
                         work_type: str = Form("regular"), notes: str = Form(""), db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
    if not crew_row:
        raise HTTPException(status_code=401, detail="Worker not found")
    try:
        hrs = float(hours)
    except (TypeError, ValueError):
        return RedirectResponse(url=request.headers.get("referer") or "/crew", status_code=status.HTTP_303_SEE_OTHER)
    if hrs <= 0 or hrs > 24:
        return RedirectResponse(url=request.headers.get("referer") or "/crew", status_code=status.HTTP_303_SEE_OTHER)
    wd = work_date or date.today().isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO timesheets (crew_id, project_id, work_date, hours, work_type, source, status, notes) "
            "VALUES (%s,%s,%s,%s,%s,'manual','submitted',%s);",
            (crew_row["id"],  project_id or None, wd, hrs, work_type, notes),
        )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/crew/hours", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/hours/{sheet_id}/delete")
async def crew_delete_hours(sheet_id: int, request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM timesheets WHERE id = %s AND crew_id = %s AND status = 'submitted';",
                    (sheet_id, worker.get("id")))
        if cur.fetchone():
            cur.execute("DELETE FROM timesheets WHERE id = %s;", (sheet_id,))
            db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/crew/hours", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/crew/photo")
async def crew_photo_upload(request: Request, project_id: int = Form(0), caption: str = Form(""),
                            punchlist: str = Form(""), photo: UploadFile = Form(...), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "photos")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM crew WHERE id = %s;", (worker.get("id"),))
        if not cur.fetchone():
            raise HTTPException(status_code=401, detail="Worker not found")
    raw = await photo.read()
    if not raw:
        return RedirectResponse(url=request.headers.get("referer") or "/crew", status_code=status.HTTP_303_SEE_OTHER)
    ext = os.path.splitext(photo.filename or "")[1][:10] or ".jpg"
    fname = f"{uuid.uuid4().hex[:12]}{ext.lower()}"
    try:
        stored = photo_store.save(raw, fname)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO job_photos (project_id, crew_id, filename, caption, is_punchlist) VALUES (%s,%s,%s,%s,%s);",
            (project_id or None, worker.get("id"), stored["key"], caption, punchlist.lower() in ("on", "true", "1")),
        )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/crew/dashboard", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/crew/hours", response_class=HTMLResponse)
async def crew_hours_page(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
        cur.execute("SELECT id, name FROM projects WHERE status IN ('planned','active');")
        jobs = cur.fetchall()
        cur.execute("""
            SELECT t.*, p.name AS project_name FROM timesheets t
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.crew_id = %s ORDER BY t.work_date DESC, t.created_at DESC LIMIT 200;
        """, (worker.get("id"),))
        sheets = cur.fetchall()
    return templates.TemplateResponse(request=request, name="crew_hours.html", context={
        "worker": crew_row, "jobs": jobs, "sheets": sheets,
    })


@app.get("/crew/pay", response_class=HTMLResponse)
async def crew_pay_page(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
        cur.execute("""
            SELECT pr.period_start, pr.period_end, pr.status AS run_status,
                   pl.hours, pl.overtime_hours, pl.gross_cents
            FROM payroll_lines pl JOIN payroll_runs pr ON pr.id = pl.run_id
            WHERE pl.crew_id = %s ORDER BY pr.period_start DESC;
        """, (worker.get("id"),))
        pay_history = cur.fetchall()
    return templates.TemplateResponse(request=request, name="crew_pay.html", context={
        "worker": crew_row, "pay_history": pay_history,
    })


@app.post("/api/crew/direct-deposit")
async def crew_direct_deposit(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE id = %s;", (worker.get("id"),))
        crew_row = cur.fetchone()
    if not crew_row:
        raise HTTPException(status_code=401, detail="Worker not found")
    if not stripe_svc.is_configured():
        return RedirectResponse(url="/crew/pay?error=Direct+deposit+is+not+enabled+yet",
                                status_code=status.HTTP_303_SEE_OTHER)
    account_id = crew_row.get("stripe_account_id")
    if not account_id:
        try:
            account_id = stripe_svc.create_worker_account(crew_row["name"], crew_row["email"], crew_row["phone"])
        except Exception:
            return RedirectResponse(url="/crew/pay?error=Could+not+create+account",
                                    status_code=status.HTTP_303_SEE_OTHER)
        with db.cursor() as cur:
            cur.execute("UPDATE crew SET stripe_account_id = %s WHERE id = %s;", (account_id, crew_row["id"]))
            db.commit()
    url = stripe_svc.account_link(account_id, str(request.base_url))
    if not url:
        return RedirectResponse(url="/crew/pay?error=Unable+to+open+onboarding",
                                status_code=status.HTTP_303_SEE_OTHER)
    return RedirectResponse(url=url)


# --- Phone app (installable PWA at /app) ------------------------------------------
# The installable phone app reuses the exact same crew auth, clock, location and
# payroll backend as the web crew portal (/crew). Logging in here sets the same
# worker_session cookie, so the app and portal share the session.

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


@app.get("/app", response_class=HTMLResponse)
async def worker_app_page(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="app.html",
        context={},
        headers={"Cache-Control": "no-cache, max-age=0"},
    )


@app.get("/manifest.webmanifest", response_class=Response)
async def web_manifest():
    with open(os.path.join(_STATIC_DIR, "manifest.webmanifest"), "r") as fh:
        content = fh.read()
    return Response(content=content, media_type="application/manifest+json")


@app.get("/sw.js", response_class=Response)
async def service_worker():
    with open(os.path.join(_STATIC_DIR, "sw.js"), "r") as fh:
        content = fh.read()
    return Response(
        content=content,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, max-age=0"},
    )


@app.post("/api/crew/app/login")
async def worker_app_login(phone: str = Form(...), pin: str = Form(...), db=Depends(get_db)):
    digits = "".join(c for c in (phone or "") if c.isdigit())
    with db.cursor() as cur:
        cur.execute("SELECT * FROM crew WHERE REGEXP_REPLACE(phone, '[^0-9]', '', 'g') = %s AND is_active = TRUE;",
                    (digits,))
        row = cur.fetchone()
    if not row or not _verify_pin((pin or "").strip(), row["pin_hash"]):
        return JSONResponse(status_code=401, content={"status": "error", "message": "Invalid phone or PIN"})
    token = auth_service.issue_session("worker", row.get("id"), row.get("email") or "", row.get("name") or "")
    resp = JSONResponse(content={"status": "ok", "name": row["name"]})
    resp.set_cookie(key=WORKER_COOKIE, value=token, httponly=True, samesite="lax", secure=_secure_cookies())
    return resp


@app.get("/api/crew/app/me")
async def worker_app_me(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    with db.cursor() as cur:
        cur.execute("SELECT id, name, phone, role, pay_type, pay_rate, is_active FROM crew WHERE id = %s;",
                    (worker.get("id"),))
        crew_row = cur.fetchone()
        if not crew_row:
            return JSONResponse(status_code=401, content={"status": "error", "message": "Not logged in"})
        cur.execute("""
            SELECT p.id, p.name, p.address, p.summary, p.status, p.start_date, p.end_date,
                   c.id AS clock_id, c.punch_in_at AS clocked_since
            FROM project_assignments pa
            JOIN projects p ON p.id = pa.project_id
            LEFT JOIN clock_events c ON c.project_id = p.id AND c.crew_id = %s AND c.punch_out_at IS NULL
            WHERE pa.crew_id = %s AND p.status IN ('planned','active')
            ORDER BY p.start_date NULLS LAST, p.created_at;
        """, (worker.get("id"), worker.get("id")))
        jobs = []
        for j in cur.fetchall():
            jobs.append({
                "id": j["id"], "name": j["name"], "address": j["address"],
                "summary": j["summary"], "status": j["status"],
                "start_date": j["start_date"].isoformat() if j["start_date"] else None,
                "end_date": j["end_date"].isoformat() if j["end_date"] else None,
                "clock_id": j["clock_id"], "clocked_since": (j["clocked_since"].isoformat() if j["clocked_since"] else None),
                "lat": None, "lng": None,
            })
        cur.execute("SELECT * FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL ORDER BY punch_in_at DESC LIMIT 1;",
                    (worker.get("id"),))
        open_clock = cur.fetchone()
        start = date.today() - timedelta(days=date.today().weekday())
        cur.execute("SELECT COALESCE(SUM(hours),0) AS h FROM timesheets WHERE crew_id = %s AND work_date >= %s;",
                    (worker.get("id"), start))
        week_hours = float(cur.fetchone()["h"] or 0)
        cur.execute("""
            SELECT t.id, t.project_id, t.work_date, t.hours, t.work_type, t.status, t.notes, p.name AS project_name
            FROM timesheets t LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.crew_id = %s ORDER BY t.work_date DESC, t.created_at DESC LIMIT 30;
        """, (worker.get("id"),))
        hours_rows = []
        for h in cur.fetchall():
            hours_rows.append({
                "id": h["id"], "project_id": h["project_id"], "project_name": h["project_name"] or "—",
                "work_date": h["work_date"].isoformat(), "hours": float(h["hours"] or 0),
                "work_type": h["work_type"], "status": h["status"],
            })
        cur.execute("""
            SELECT pl.hours, pl.overtime_hours, pl.gross_cents, pr.period_start, pr.period_end, pr.status AS run_status
            FROM payroll_lines pl JOIN payroll_runs pr ON pr.id = pl.run_id
            WHERE pl.crew_id = %s ORDER BY pr.period_start DESC LIMIT 12;
        """, (worker.get("id"),))
        pay_rows = []
        for p in cur.fetchall():
            pay_rows.append({
                "hours": float(p["hours"] or 0),
                "overtime_hours": float(p["overtime_hours"] or 0),
                "gross_cents": int(p["gross_cents"] or 0),
                "period_start": p["period_start"].isoformat(),
                "period_end": p["period_end"].isoformat(),
                "run_status": p["run_status"],
            })
    return JSONResponse(content={
        "status": "ok",
        "worker": {"id": crew_row["id"], "name": crew_row["name"], "phone": crew_row["phone"],
                   "role": crew_row["role"], "pay_rate": float(crew_row["pay_rate"] or 0)},
        "features": _crew_features(db),
        "jobs": jobs,
        "week_hours": week_hours,
        "open_clock": open_clock and {"id": open_clock["id"], "project_id": open_clock["project_id"],
                                      "punch_in_at": open_clock["punch_in_at"].isoformat()},
        "hours": hours_rows,
        "pay": pay_rows,
    })


@app.post("/api/crew/app/clock")
async def worker_app_clock(request: Request, project_id: int = Form(0), action: str = Form(...),
                           lat: str = Form(""), lng: str = Form(""), accuracy: str = Form(""), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "timeclock")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM crew WHERE id = %s AND is_active = TRUE;", (worker.get("id"),))
        if not cur.fetchone():
            return JSONResponse(status_code=401, content={"status": "error", "message": "Worker not found"})

    if action == "in":
        with db.cursor() as cur:
            cur.execute("SELECT id FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL;", (worker.get("id"),))
            if cur.fetchone():
                return JSONResponse(content={"status": "error", "message": "Already clocked in"})
            cur.execute(
                "INSERT INTO clock_events (crew_id, project_id, punch_in_at, punch_in_lat, punch_in_lng) "
                "VALUES (%s,%s,NOW(),%s,%s) RETURNING id;",
                (worker.get("id"), project_id or None, lat or None, lng or None),
            )
            db.commit()
        return JSONResponse(content={"status": "ok"})

    with db.cursor() as cur:
        ev = None
        if action == "out":
            cur.execute("SELECT * FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL ORDER BY punch_in_at DESC LIMIT 1;",
                        (worker.get("id"),))
            ev = cur.fetchone()
            if not ev:
                return JSONResponse(content={"status": "error", "message": "Not clocked in"})
            cur.execute("UPDATE clock_events SET punch_out_at = NOW(), punch_out_lat = %s, punch_out_lng = %s WHERE id = %s;",
                        (lat or None, lng or None, ev["id"]))
            db.commit()
            in_dt = ev["punch_in_at"]
            out_dt = datetime.now(APP_TZ)
            hours = max(round((out_dt - in_dt).total_seconds() / 3600, 2), 0)
            cur.execute(
                "INSERT INTO timesheets (crew_id, project_id, work_date, start_time, end_time, hours, work_type, source, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'regular','clock','submitted');",
                (worker.get("id"), ev["project_id"], in_dt.date(), in_dt.strftime("%H:%M"),
                 out_dt.strftime("%H:%M"), hours),
            )
            db.commit()
        elif action == "update":
            cur.execute("SELECT * FROM clock_events WHERE crew_id = %s AND punch_out_at IS NULL ORDER BY punch_in_at DESC LIMIT 1;",
                        (worker.get("id"),))
            ev = cur.fetchone()
            if ev:
                cur.execute("UPDATE clock_events SET punch_in_lat = %s, punch_in_lng = %s WHERE id = %s;",
                            (lat or None, lng or None, ev["id"]))
                db.commit()
        else:
            return JSONResponse(status_code=400, content={"status": "error", "message": "Unknown action"})
    return JSONResponse(content={"status": "ok"})


@app.post("/api/crew/app/hours")
async def worker_app_hours(request: Request, project_id: int = Form(0), work_date: str = Form(""),
                           hours: str = Form(""), work_type: str = Form("regular"), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "hours")
    try:
        hrs = float(hours)
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"status": "error", "message": "Invalid hours"})
    if hrs <= 0 or hrs > 24:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Hours must be 0-24"})
    wd = work_date or date.today().isoformat()
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO timesheets (crew_id, project_id, work_date, hours, work_type, source, status) "
            "VALUES (%s,%s,%s,%s,%s,'manual','submitted');",
            (worker.get("id"), project_id or None, wd, hrs, work_type),
        )
        db.commit()
    return JSONResponse(content={"status": "ok"})


@app.post("/api/crew/app/hours/{sheet_id}/delete")
async def worker_app_hours_delete(sheet_id: int, request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "hours")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM timesheets WHERE id = %s AND crew_id = %s AND status = 'submitted';",
                    (sheet_id, worker.get("id")))
        if cur.fetchone():
            cur.execute("DELETE FROM timesheets WHERE id = %s;", (sheet_id,))
            db.commit()
            return JSONResponse(content={"status": "ok"})
    return JSONResponse(status_code=404, content={"status": "error", "message": "Not found"})


@app.post("/api/crew/app/logout")
async def worker_app_logout():
    resp = JSONResponse(content={"status": "ok"})
    resp.delete_cookie(WORKER_COOKIE)
    return resp


# --- Crew app: Bulletin alerts --------------------------------------------------
BULLETIN_KEY = "crew_bulletin"


@app.get("/api/crew/app/bulletin")
async def worker_app_bulletin(request: Request, db=Depends(get_db)):
    require_worker(request)
    text = _get_setting(db, BULLETIN_KEY, "")
    return JSONResponse(content={"text": text or None})


# --- Crew app: Photos ----------------------------------------------------------
@app.get("/api/crew/app/photos")
async def worker_app_photos(request: Request, project_id: int = 0, db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "photos")
    with db.cursor() as cur:
        if project_id:
            cur.execute("""
                SELECT jp.id, jp.project_id, p.name AS project_name, jp.crew_id, c.name AS crew_name,
                       jp.caption, jp.is_punchlist, jp.filename, jp.created_at
                FROM job_photos jp
                LEFT JOIN projects p ON p.id = jp.project_id
                LEFT JOIN crew c ON c.id = jp.crew_id
                WHERE jp.project_id = %s ORDER BY jp.created_at DESC LIMIT 200;
            """, (project_id,))
        else:
            cur.execute("""
                SELECT jp.id, jp.project_id, p.name AS project_name, jp.crew_id, c.name AS crew_name,
                       jp.caption, jp.is_punchlist, jp.filename, jp.created_at
                FROM job_photos jp
                JOIN project_assignments pa ON pa.project_id = jp.project_id AND pa.crew_id = %s
                LEFT JOIN projects p ON p.id = jp.project_id
                LEFT JOIN crew c ON c.id = jp.crew_id
                ORDER BY jp.created_at DESC LIMIT 200;
            """, (worker.get("id"),))
        photos = []
        for r in cur.fetchall():
            photos.append({
                "id": r["id"], "project_id": r["project_id"], "project_name": r["project_name"],
                "crew_id": r["crew_id"], "crew_name": r["crew_name"], "caption": r["caption"],
                "is_punchlist": bool(r["is_punchlist"]),
                "url": photo_store.url_for(r["filename"]),
                "created_at": r["created_at"].isoformat(),
            })
    return JSONResponse(content={"photos": photos})


@app.post("/api/crew/app/photos")
async def worker_app_photos_upload(request: Request, project_id: int = Form(0), caption: str = Form(""),
                                   punchlist: str = Form(""), photo: UploadFile = Form(...), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "photos")
    with db.cursor() as cur:
        cur.execute("SELECT id FROM crew WHERE id = %s;", (worker.get("id"),))
        if not cur.fetchone():
            raise HTTPException(status_code=401, detail="Worker not found")
    raw = await photo.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    ext = os.path.splitext(photo.filename or "")[1][:10] or ".jpg"
    fname = f"{uuid.uuid4().hex[:12]}{ext.lower()}"
    try:
        stored = photo_store.save(raw, fname)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO job_photos (project_id, crew_id, filename, caption, is_punchlist) VALUES (%s,%s,%s,%s,%s);",
            (project_id or None, worker.get("id"), stored["key"], caption, punchlist.lower() in ("on", "true", "1")),
        )
        db.commit()
    return JSONResponse(content={"status": "ok", "url": stored["url"],
                                 "key": stored["key"], "backend": stored["backend"],
                                 "persistent": stored["backend"] == "r2"})


# --- Crew app: Messages with the office ---------------------------------------
# --- Team messages: owner <-> crew -------------------------------------------
# One module (team_messages) owns the table, the visibility rule and the
# serializer. The crew PWA keeps its /api/crew/app/messages endpoints and
# payload shape; the owner gets /messages. Both share the authenticated
# WebSocket below.

def _message_actor(conn_or_ws) -> dict | None:
    """Admin session or crew session -> {role, id, name}; None otherwise.

    Works for both Request and WebSocket (both expose .cookies).
    """
    return team_messages.normalize_actor(current_actor(conn_or_ws), current_worker(conn_or_ws))


MSG_CLIENTS: dict = {}   # WebSocket -> actor


async def _push_message(row: dict) -> None:
    """Fan one serialized message out to every socket allowed to see it."""
    for ws, viewer in list(MSG_CLIENTS.items()):
        try:
            if team_messages.visible(row, viewer):
                await ws.send_json(row)
        except Exception:
            MSG_CLIENTS.pop(ws, None)


@app.get("/api/crew/app/messages")
async def worker_app_messages(request: Request, after: int = 0, db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "messages")
    actor = team_messages.normalize_actor(None, worker)
    return JSONResponse(content={"messages": team_messages.list_after(db, after, actor)})


@app.post("/api/crew/app/messages")
async def worker_app_messages_send(request: Request, body: str = Form(...), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "messages")
    actor = team_messages.normalize_actor(None, worker)
    try:
        row = team_messages.send(db, actor, body)
    except ValueError as e:
        return JSONResponse(content={"status": "error", "error": str(e)}, status_code=400)
    await _push_message(row)
    return JSONResponse(content={"status": "ok", "id": row["id"]})


@app.get("/messages", response_class=HTMLResponse)
async def messages_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Owner login required")
    return templates.TemplateResponse(request=request, name="messages.html", context={
        "user": {"email": user_email}, "actor": actor,
    })


@app.get("/api/messages")
async def messages_list(request: Request, after: int = 0, db=Depends(get_db)):
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    return JSONResponse(content={"ok": True, "messages": team_messages.list_after(db, after, actor)})


@app.get("/api/messages/roster")
async def messages_roster(request: Request, db=Depends(get_db)):
    require_admin(request)
    return JSONResponse(content={"ok": True, "roster": team_messages.roster(db)})


@app.post("/api/messages")
async def messages_send(request: Request, body: str = Form(...), kind: str = Form("dm"),
                        to_role: str = Form(""), to_id: str = Form(""), db=Depends(get_db)):
    actor = _message_actor(request)
    if not actor:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Login required")
    try:
        row = team_messages.send(db, actor, body, kind=kind, to_role=to_role, to_id=to_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    await _push_message(row)
    return JSONResponse(content={"ok": True, "message": row})


@app.post("/api/messages/clear")
async def messages_clear(request: Request, db=Depends(get_db)):
    require_admin(request)
    cleared = team_messages.clear(db)
    for ws in list(MSG_CLIENTS):
        try:
            await ws.send_json({"clear": True})
        except Exception:
            MSG_CLIENTS.pop(ws, None)
    return JSONResponse(content={"ok": True, "cleared": cleared})


@app.websocket("/ws/messages")
async def ws_messages(websocket: WebSocket):
    """Live feed. Authenticated, and filtered per viewer.

    The previous socket accepted anyone and broadcast every crew DM to every
    connection, so an anonymous client could read the office inbox. Now a
    session is required to connect (close 1008 otherwise -- the crew PWA
    already treats that code as "locked out"), and each push goes through
    team_messages.visible for that viewer. A client may send its last seen
    id as text to get a replay of what it missed.
    """
    actor = _message_actor(websocket)
    if not actor:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    MSG_CLIENTS[websocket] = actor
    try:
        while True:
            text = await websocket.receive_text()
            try:
                after_id = int(text)
            except (TypeError, ValueError):
                continue
            conn = psycopg.connect(db_url, row_factory=dict_row)
            try:
                for row in team_messages.list_after(conn, after_id, actor, limit=200):
                    await websocket.send_json(row)
            finally:
                conn.close()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        MSG_CLIENTS.pop(websocket, None)


# --- Training: safety orientation (OSHA-10 baseline) + trade skills tests -------------
# Crew portal page and phone-app API. Content lives in training_service.py. Company
# safety orientation module covers OSHA implementing rules (29 CFR 1926) and site
# safety requirements. It is informational company training — NOT official OSHA-10/30
# certification, which only an authorized OSHA trainer can issue.
TRAINING_DISCLAIMER = (f"This is {brand.short_name()} company training covering OSHA "
                       "implementing rules (29 CFR 1926) and site safety requirements. "
                       "It is informational and does NOT certify or replace official "
                       "OSHA-10/OSHA-30 training, which only an authorized OSHA trainer can provide.")


@app.get("/crew/training", response_class=HTMLResponse)
async def crew_training_page(request: Request):
    require_worker(request)
    worker = request.state.worker if hasattr(request.state, "worker") else None
    decks = [
        {
            "kind": "construction-trades",
            "name": "Tools of the Trade",
            "blurb": "Tape measure, math, framing, drywall, roofing, plumbing, electrical, HVAC, tile, painting, concrete.",
            "slides": training_service.deck_slides("construction-trades"),
        },
        {
            "kind": "construction-safety",
            "name": "Job Site Safety (OSHA-10)",
            "blurb": "The Fatal Four, fall protection, PPE, chemicals, LOTO, trenches, silica & asbestos.",
            "slides": training_service.deck_slides("construction-safety"),
        },
        {
            "kind": "construction-app",
            "name": "Crew App — Install & Time Reporting",
            "blurb": "Install the app, log in, jobs & map, clock in/out, time reporting & overtime, direct deposit.",
            "slides": training_service.deck_slides("construction-app"),
        },
        {
            "kind": "construction-ethics",
            "name": "Workplace Ethics & Conduct",
            "blurb": "Honesty, time reporting, client trust, confidentiality, zero tolerance for harassment.",
            "slides": training_service.deck_slides("construction-ethics"),
        },
    ]
    return templates.TemplateResponse(
        request=request,
        name="training.html",
        context={
            "disclaimer": TRAINING_DISCLAIMER,
            "decks": decks,
            "worker": worker or {},
        },
        headers={"Cache-Control": "no-cache, max-age=0"},
    )


DECK_FILENAMES = {
    "construction": "crew-safety-orientation.pptx",
    "construction-trades": "tools-of-the-trade.pptx",
    "construction-safety": "job-site-safety-osha10.pptx",
    "construction-app": "crew-app-install-and-time-reporting.pptx",
    "construction-ethics": "workplace-ethics-and-conduct.pptx",
    "worker": "worker-orientation.pptx",
}


@app.get("/crew/training/ppt")
@app.get("/crew/training/ppt/{deck}")
async def crew_training_ppt(request: Request, db=Depends(get_db), deck: str = "construction"):
    require_worker(request)
    kind = training_service.CONSTRUCTION_DECK_KINDS.get(deck, deck)
    if kind not in DECK_FILENAMES:
        return JSONResponse(status_code=404, content={"ok": False, "error": "Unknown deck"})
    try:
        data = training_service.build_deck(kind)
    except Exception as e:
        return JSONResponse(status_code=500, content={"ok": False, "error": f"Could not build deck: {e}"})
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    headers={"Content-Disposition": f"attachment; filename={DECK_FILENAMES[kind]}"})


@app.get("/crew/training/content")
async def crew_training_content(request: Request, db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "training")
    with db.cursor() as cur:
        cur.execute(
            "SELECT test_slug, test_title, score, total, passed, created_at "
            "FROM worker_quiz_results WHERE worker_id = %s ORDER BY created_at DESC;",
            (worker.get("id"),),
        )
        history = [
            {
                "test_slug": r["test_slug"] or "osha",
                "test_title": r["test_title"] or "Site Safety Orientation (OSHA-10 baseline)",
                "score": r["score"], "total": r["total"], "passed": r["passed"],
                "at": r["created_at"].isoformat(),
            }
            for r in cur.fetchall()
        ]
    return JSONResponse(content={
        "status": "ok",
        "disclaimer": TRAINING_DISCLAIMER,
        "tests": [
            {"slug": t["slug"], "title": t["title"], "count": len(t["questions"]),
             "questions": [{"q": q["q"], "options": q.get("options"), "topic": q.get("topic"),
                            "type": q.get("type"), "accept": q.get("accept")} for q in t["questions"]]}
            for t in training_service.ALL_TRAININGS
        ],
        "history": history,
    })


@app.post("/api/crew/app/training/submit")
async def crew_training_submit(request: Request, test_slug: str = Form(...), answers: str = Form(...), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "training")
    test = training_service.TRAINING_BY_SLUG.get(test_slug)
    if not test:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Unknown test"})
    try:
        parsed = json.loads(answers)
        if not isinstance(parsed, list):
            raise ValueError("not a list")
        grade = training_service.grade_training(parsed, test["questions"])
    except ValueError as e:
        return JSONResponse(status_code=400, content={"status": "error", "message": str(e)})
    with db.cursor() as cur:
        cur.execute("SELECT name FROM crew WHERE id = %s;", (worker.get("id"),))
        row = cur.fetchone()
        name = row["name"] if row else "Crew member"
        cur.execute(
            "INSERT INTO worker_quiz_results (worker_id, worker_name, score, total, passed, answers_json, test_slug, test_title) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (worker.get("id"), name, grade["correct"], grade["total"], grade["passed"], answers,
             test["slug"], test["title"]),
        )
        result_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse(content={
        "status": "ok",
        "result_id": result_id,
        "test_slug": test["slug"],
        "test_title": test["title"],
        "total": grade["total"], "correct": grade["correct"], "percent": grade["percent"],
        "passed": grade["passed"], "missed_topics": grade["missed_topics"],
    })


# --- Payroll ----------------------------------------------------------------
def _default_pay_period():
    end = date.today()
    start = end - timedelta(days=13)
    return start, end


def _compute_gross(rate: float, sheets: list) -> tuple:
    weekly = {}
    for t in sheets:
        iso = t["work_date"].isocalendar()
        week = (iso[0], iso[1])
        weekly[week] = weekly.get(week, 0.0) + float(t["hours"] or 0)
    reg = sum(min(h, 40.0) for h in weekly.values())
    ot = sum(max(h - 40.0, 0.0) for h in weekly.values())
    gross = reg * float(rate) + ot * float(rate) * 1.5
    return round(reg, 2), round(ot, 2), int(round(gross * 100))


@app.get("/payroll", response_class=HTMLResponse)
async def payroll_page(request: Request, period_start: str = "", period_end: str = "", db=Depends(get_db)):
    require_admin(request)
    try:
        ps = date.fromisoformat(period_start) if period_start else None
    except ValueError:
        ps = None
    try:
        pe = date.fromisoformat(period_end) if period_end else None
    except ValueError:
        pe = None
    if not ps or not pe:
        ps, pe = _default_pay_period()
    with db.cursor() as cur:
        cur.execute("""
            SELECT t.crew_id, c.name, c.pay_type, c.pay_rate,
                   COALESCE(SUM(t.hours),0) AS h,
                   COUNT(t.id) AS ent
            FROM timesheets t JOIN crew c ON c.id = t.crew_id
            WHERE t.work_date BETWEEN %s AND %s AND t.status IN ('submitted','approved')
            GROUP BY t.crew_id, c.name, c.pay_type, c.pay_rate
            ORDER BY c.name;
        """, (ps, pe))
        crew_totals = cur.fetchall()
        cur.execute("""
            SELECT t.*, c.name AS crew_name, p.name AS project_name FROM timesheets t
            JOIN crew c ON c.id = t.crew_id
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.work_date BETWEEN %s AND %s ORDER BY t.work_date, c.name;
        """, (ps, pe))
        sheets = cur.fetchall()
        cur.execute("""
            SELECT pr.*, COALESCE(SUM(pl.gross_cents),0) AS gross
            FROM payroll_runs pr LEFT JOIN payroll_lines pl ON pl.run_id = pr.id
            GROUP BY pr.id ORDER BY pr.period_start DESC LIMIT 12;
        """)
        runs = cur.fetchall()
    return templates.TemplateResponse(request=request, name="payroll.html", context={
        "user": {"email": require_auth(request)[1]},
        "period_start": ps.isoformat(), "period_end": pe.isoformat(),
        "crew_totals": crew_totals, "sheets": sheets, "runs": runs,
    })


@app.post("/api/payroll/timesheets/{sheet_id}/approve")
async def payroll_sheet_approve(sheet_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE timesheets SET status = 'approved' WHERE id = %s;", (sheet_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/payroll", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/payroll/timesheets/{sheet_id}/reject")
async def payroll_sheet_reject(sheet_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE timesheets SET status = 'rejected' WHERE id = %s;", (sheet_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/payroll", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/payroll/run")
async def payroll_run_create(request: Request, period_start: str = Form(...), period_end: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    ps = date.fromisoformat(period_start)
    pe = date.fromisoformat(period_end)
    with db.cursor() as cur:
        cur.execute("SELECT id FROM payroll_runs WHERE period_start = %s AND period_end = %s AND status = 'open';", (ps, pe))
        if cur.fetchone():
            return RedirectResponse(url=f"/payroll?period_start={ps}&period_end={pe}&error=run+exists",
                                    status_code=status.HTTP_303_SEE_OTHER)
        cur.execute("INSERT INTO payroll_runs (period_start, period_end, status) VALUES (%s,%s,'open') RETURNING id;", (ps, pe))
        run_id = cur.fetchone()["id"]
        cur.execute("""
            SELECT t.crew_id, c.pay_type, c.pay_rate FROM timesheets t
            JOIN crew c ON c.id = t.crew_id
            WHERE t.work_date BETWEEN %s AND %s AND t.status IN ('submitted','approved')
            GROUP BY t.crew_id, c.pay_type, c.pay_rate;
        """, (ps, pe))
        crew_rows = cur.fetchall()
        jobs_done = 0
        for crew in crew_rows:
            cur.execute("SELECT work_date, hours FROM timesheets WHERE crew_id = %s AND work_date BETWEEN %s AND %s "
                        "AND status IN ('submitted','approved');", (crew["crew_id"], ps, pe))
            sheets = cur.fetchall()
            if not sheets:
                continue
            if crew["pay_type"] == "salary":
                reg, ot, gross = 0, 0, int(round(float(crew["pay_rate"]) * 100))
            else:
                reg, ot, gross = _compute_gross(float(crew["pay_rate"]), sheets)
            cur.execute(
                "INSERT INTO payroll_lines (run_id, crew_id, hours, overtime_hours, gross_cents) VALUES (%s,%s,%s,%s,%s);",
                (run_id, crew["crew_id"], reg, ot, gross),
            )
            cur.execute("UPDATE timesheets SET status = 'paid' WHERE crew_id = %s AND work_date BETWEEN %s AND %s "
                        "AND status IN ('submitted','approved');", (crew["crew_id"], ps, pe))
            jobs_done += 1
        db.commit()
    # Accrue the wages at run creation, not at payout. From this moment the
    # crew are owed the money whether or not it has been sent, and a job's
    # margin is wrong for the whole period if it waits until the money moves.
    try:
        ledger_sync.ensure_schema(db)
        ledger_sync.sync_payroll_accrual(db, run_id, company=_BID_COMPANY)
    except Exception as e:
        logger.warning("ledger not updated for payroll run %s: %s", run_id, e)
    return RedirectResponse(url=f"/payroll?period_start={ps}&period_end={pe}&ok=run+created+{jobs_done}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/payroll/run/{run_id}/finalize")
async def payroll_run_finalize(run_id: int, request: Request, db=Depends(get_db)):
    """Close a run without paying it.

    Deliberately does NOT relieve the payroll liability. Closing a run only
    stops further hours being added; if the money has not moved the crew are
    still owed it and the books should still say so. Paying settles it, and
    that path posts its own entry.
    """
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE payroll_runs SET status = 'closed' WHERE id = %s;", (run_id,))
        db.commit()
    try:
        ledger_sync.ensure_schema(db)
        ledger_sync.sync_payroll_accrual(db, run_id, company=_BID_COMPANY)
    except Exception as e:
        logger.warning("ledger not updated for payroll run %s: %s", run_id, e)
    return RedirectResponse(url=request.headers.get("referer") or "/payroll", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/leads/{lead_id}/enrich-contact")
async def enrich_lead_contact(lead_id: int, request: Request, db=Depends(get_db)):
    """Name + address -> email and phone, via Accurate Append. Admin-only.

    This is the step the owner lookup cannot do. RentCast returns an owner NAME
    from the assessor record; no public record contains an email address, so
    the name has to be turned into a contact detail by a consumer contact
    database. Costs up to two lookups against a monthly quota, so the result is
    cached on the lead and a repeat is free.

    Does not send anything. Phones are recorded but never dialled -- these are
    residential numbers and a cold call needs a DNC check first.

    Currently returns a 502 with the provider's own message: the configured key
    is recognised but the account has no active subscription, so every
    endpoint is unauthorized. See MEMORY.md.
    """
    require_admin(request)
    _, user_email = require_auth(request)

    with db.cursor() as cur:
        cur.execute("SELECT id, name, email, phone, address FROM leads WHERE id = %s;", (lead_id,))
        lead = cur.fetchone()
    if not lead:
        raise HTTPException(status_code=404, detail="No such lead")

    # Skip if we already hold both. Caching here is what keeps a monthly quota
    # from being spent re-discovering the same contact.
    have_email = (lead.get("email") or "").strip() and "@lead.local" not in (lead.get("email") or "")
    have_phone = bool((lead.get("phone") or "").strip())
    if have_email and have_phone:
        return JSONResponse(content={"ok": True, "skipped": True,
                                     "why": "this lead already has an email and a phone",
                                     "email": lead.get("email"), "phone": lead.get("phone")})

    full = (lead.get("name") or "").strip()
    if not full or full.lower() == "the homeowner":
        raise HTTPException(status_code=400,
                            detail="This lead has no owner name yet. Run the owner lookup first.")
    parts = full.split()
    first, last = parts[0], parts[-1]

    address = (lead.get("address") or "").strip()
    parsed = skiptrace_service.parse_address(address)
    if not parsed["ok"]:
        raise HTTPException(status_code=400,
                            detail=f"Cannot read this lead's address ({parsed['why']}).")

    result = await asyncio.to_thread(
        accurate_append.enrich_lead,
        first, last, parsed["street"], parsed["city"], parsed["state"], parsed["zipcode"])

    email = (result.get("email") or {}).get("email") or ""
    phone = (result.get("mobile") or {}).get("phone") or ""
    if not email and not phone:
        detail = ((result.get("email") or {}).get("error")
                  or (result.get("mobile") or {}).get("error") or "no match")
        # 424, not 502. Cloudflare substitutes its own plain-text error page for
        # a 502 and discards the response body, which threw away the provider's
        # actual explanation -- "no active subscription" is the whole reason this
        # is blocked, and a bare "error code: 502" is useless to whoever needs to
        # fix it. Failed Dependency is also the semantically correct code for "an
        # upstream refused us".
        raise HTTPException(status_code=424, detail=detail)

    # Never overwrite a real existing value; only fill blanks and @lead.local
    # placeholders, matching how skip_sherpa writes results back.
    with db.cursor() as cur:
        cur.execute(
            "UPDATE leads SET "
            "  email = CASE WHEN %s <> '' AND (BTRIM(COALESCE(email,'')) = '' "
            "                OR LOWER(email) LIKE '%%@lead.local') THEN %s ELSE email END, "
            "  phone = CASE WHEN %s <> '' AND BTRIM(COALESCE(phone,'')) = '' THEN %s ELSE phone END "
            "WHERE id = %s RETURNING id, email, phone;",
            (email, email, phone, phone, lead_id),
        )
        saved = cur.fetchone()
        cur.execute(
            "UPDATE leads SET analysis_json = CASE WHEN analysis_json IS NULL THEN %s::jsonb "
            "  ELSE analysis_json::jsonb || %s::jsonb END WHERE id = %s;",
            (json.dumps({"accurate_append": {"at": result["checked_at"],
                                             "email_match": (result.get("email") or {}).get("match_level"),
                                             "phone_match": (result.get("mobile") or {}).get("match_level")}}),
             json.dumps({"accurate_append": {"at": result["checked_at"]}}), lead_id),
        )
        db.commit()

    return JSONResponse(content={
        "ok": True,
        "lead_id": lead_id,
        "email": saved["email"],
        "phone": saved["phone"],
        "email_match_level": (result.get("email") or {}).get("match_level"),
        "phone_match_level": (result.get("mobile") or {}).get("match_level"),
        "rejected_weak_matches": ((result.get("email") or {}).get("rejected_weak", 0)
                                  + (result.get("mobile") or {}).get("rejected_weak", 0)),
        "note": "Phone recorded, never auto-dialled. A DNC check is required before calling.",
    })


@app.get("/api/leads/addresses.csv")
async def leads_addresses_csv(request: Request, company: str = "construction",
                              db=Depends(get_db)):
    """Every lead that has a street address, as CSV. Admin-only.

    This is the working list for mail: the permit-derived leads publish an
    address and no applicant, so `name` holds a city for most of them. Two
    things are worth knowing before using the file.

    First, DUPLICATES. The permit feeds emit one row per permit, so several
    rows share one address -- 177 leads covered only 58 distinct doors in the
    September batch. The `distinct_addresses` column marks the first row seen
    for each address so a mailing run can filter on it rather than paying twice
    for the same person.

    Second, `has_contact` marks whether the lead already has a usable phone or
    email. Those do not need chasing; they are already contactable and appear in
    the contactable lane instead.

    Sorted by address so related rows sit together, and the export is capped at
    5000 rows: past that a spreadsheet stops being usable and a paginated
    export is the right answer.
    """
    require_admin(request)
    co = "construction"
    with db.cursor() as cur:
        # No city/state/zip columns on `leads` -- the whole postal address lives
        # in `address` as one string. (job_leads has city/state; leads does
        # not.) Selecting them was a 500 on every request.
        cur.execute(
            "SELECT id, name, address, email, phone, status, source, "
            "       project_type, created_at "
            "FROM leads "
            "WHERE company = %(company)s AND address IS NOT NULL AND BTRIM(address) <> '' "
            "ORDER BY LOWER(BTRIM(address)), id "
            "LIMIT 5000;",
            {"company": co},
        )
        rows = cur.fetchall()

    def has_contact(r):
        email = (r.get("email") or "").strip()
        phone = (r.get("phone") or "").strip().lower()
        if email and "@lead.local" not in email.lower():
            return True
        return bool(phone and phone not in ("unknown", "n/a", "-", "none"))

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "lead_id", "name", "address", "email", "phone", "has_contact",
        "status", "source", "project_type", "first_row_for_address",
        "distinct_addresses", "created_at",
    ])

    def addr_key(value):
        # Collapse whitespace and case so one house is one key. "  8494  Lynn
        # River ROAD " and "8494 Lynn River Road" are the same door.
        return " ".join((value or "").split()).strip().lower()

    totals = {}
    for r in rows:
        key = addr_key(r.get("address"))
        totals[key] = totals.get(key, 0) + 1

    # `first_row_for_address` marks the first row SEEN for each address, so a
    # mailing run can filter to one row per door. Comparing against the total
    # instead would mark every row of a duplicated address as "no" and leave
    # nothing to filter on.
    first_seen = set()
    written = 0
    for r in rows:
        key = addr_key(r.get("address"))
        first = key not in first_seen
        first_seen.add(key)
        w.writerow([
            r.get("id"),
            (r.get("name") or "").strip(),
            (r.get("address") or "").strip(),
            (r.get("email") or "").strip(),
            (r.get("phone") or "").strip(),
            "yes" if has_contact(r) else "no",
            (r.get("status") or "").strip(),
            (r.get("source") or "").strip(),
            (r.get("project_type") or "").strip(),
            "yes" if first else "no",
            totals[key],
            r.get("created_at").isoformat() if r.get("created_at") else "",
        ])
        written += 1

    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{co}-leads-with-addresses.csv"',
            "X-Row-Count": str(written),
            "X-Distinct-Addresses": str(len(totals)),
        },
    )


@app.get("/api/payroll/run/{run_id}/csv")
async def payroll_run_csv(run_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM payroll_runs WHERE id = %s;", (run_id,))
        run = cur.fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        cur.execute("""
            SELECT c.name, c.role, c.pay_type, c.pay_rate, pl.hours, pl.overtime_hours, pl.gross_cents
            FROM payroll_lines pl JOIN crew c ON c.id = pl.crew_id
            WHERE pl.run_id = %s ORDER BY c.name;
        """, (run_id,))
        lines = cur.fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Pay period", str(run["period_start"]), str(run["period_end"]), "Status", run["status"]])
    w.writerow([])
    w.writerow(["Name", "Role", "Pay type", "Rate", "Regular hours", "Overtime hours", "Gross pay"])
    for l in lines:
        w.writerow([l["name"], l["role"], l["pay_type"], str(l["pay_rate"]) if l["pay_rate"] is not None else "",
                    l["hours"], l["overtime_hours"], f"${l['gross_cents']/100:.2f}"])
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="payroll-{run["period_start"]}-to-{run["period_end"]}.csv"'},
    )


@app.post("/api/payroll/run/{run_id}/pay/{crew_id}")
async def payroll_pay_worker(run_id: int, crew_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM payroll_lines WHERE run_id = %s AND crew_id = %s;", (run_id, crew_id))
        line = cur.fetchone()
        cur.execute("SELECT * FROM crew WHERE id = %s;", (crew_id,))
        crew = cur.fetchone()
    if not line or not crew:
        raise HTTPException(status_code=404, detail="Pay line or crew not found")
    ok = stripe_svc.transfer_to_worker(crew, line["gross_cents"])
    if not ok:
        return RedirectResponse(url=request.headers.get("referer") or "/payroll",
                                status_code=status.HTTP_303_SEE_OTHER)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO payments (lead_id, stripe_payment_intent_id, amount_cents, currency, status, company) "
            "VALUES (NULL, %s, %s, 'usd', 'paid', 'construction');",
            (f"payroll-run-{run_id}-crew-{crew_id}", line["gross_cents"]),
        )
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/payroll?ok=paid+%23" + str(crew_id),
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/payments/connect-webhook")
async def connect_webhook(request: Request, db=Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        event = stripe_svc.construct_connect_event(payload, sig)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Connect webhook failed: {e}")
    if isinstance(event, dict):
        etype = event.get("type")
        eobj = (event.get("data") or {}).get("object") or {}
    else:
        etype = event.type
        eobj = event.data.object if event.data else {}
    if etype in ("account.updated",):
        # stripe>=8 objects are NOT mappings: eobj.get() raises AttributeError
        # (->500 on every Connect event, so Stripe retries and gives up). Same
        # fix as the payments webhook above; to_dict() exists on StripeObject
        # but not on a plain dict, hence the hasattr guard.
        eobj = eobj.to_dict() if hasattr(eobj, "to_dict") else dict(eobj)
        account_id = eobj.get("id")
        payouts_enabled = bool(eobj.get("payouts_enabled"))
        with db.cursor() as cur:
            cur.execute("UPDATE crew SET bank_status = %s WHERE stripe_account_id = %s;",
                        ("connected" if payouts_enabled else "pending", account_id))
            db.commit()
    return Response(content='{"received": true}', media_type="application/json")


# --- Owner copilot ----------------------------------------------------------
@app.get("/copilot", response_class=HTMLResponse)
async def copilot_page(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response = templates.TemplateResponse(
        request=request, name="copilot.html", context={"user": {"email": user_email}})
    # This page's JavaScript decides how a failure is displayed, so a cached copy
    # is a correctness problem and not just a stale asset: a browser holding the
    # pre-fix version renders every 503 as an empty "(no response)" bubble,
    # because it only ever looks at json.reply. The Copilot is an operator tool
    # used one at a time, so re-fetching it costs nothing.
    response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


def _copilot_owner_email(request: Request) -> str:
    """Require an admin session and return its email as the memory scope key."""
    require_admin(request)
    email = str((current_actor(request) or {}).get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No owner email on session")
    return email


@app.post("/api/copilot")
async def copilot_chat(request: Request, message: str = Form(...), db=Depends(get_db)):
    owner_email = _copilot_owner_email(request)
    history = copilot_memory.load_history(db, "copilot", owner_email)
    agent = BusinessAIAgent(
        knowledge_path="construction_knowledge.md",
        tool_handlers=build_copilot_handlers(db, owner_email),
        subset="copilot",
    )
    try:
        # Off the event loop: a Copilot turn can run tools for up to
        # COPILOT_TOOL_DEADLINE_SECONDS (180 in production). Synchronous here,
        # that was 180 seconds during which no homeowner could load any page.
        reply = await asyncio.to_thread(agent.process_conversation, history, message)
    except ToolBudgetExhausted as exc:
        # Surfaced as a real error on purpose. Returning the fallback string here
        # is what made this look like missing data: the agent would report it
        # "couldn't access the information" and the owner would go hunting through
        # lead records when the actual fault was one tool failing repeatedly.
        print(f"🚨 Copilot tool budget exhausted: {exc}")
        return JSONResponse(
            status_code=502,
            content={"error": str(exc), "tooling": True},
        )
    except ModelUnavailable as exc:
        # Almost always an exhausted OpenAI balance. The old fallback told the
        # owner "Message received. Our team will follow up with you shortly",
        # which reads like a working assistant with nothing to say.
        print(f"🚨 Copilot model unavailable: {exc}")
        return JSONResponse(
            status_code=503,
            content={"error": str(exc), "billing": True},
        )
    copilot_memory.append_turn(db, "copilot", owner_email, "user", message)
    copilot_memory.append_turn(db, "copilot", owner_email, "assistant", reply)
    return JSONResponse(content={"reply": reply})


@app.get("/api/copilot/history")
async def copilot_history(request: Request, db=Depends(get_db)):
    """Replay the stored conversation so a refresh doesn't lose the thread."""
    owner_email = _copilot_owner_email(request)
    return JSONResponse(
        content={"messages": copilot_memory.load_history(db, "copilot", owner_email, limit=200)}
    )


@app.post("/api/copilot/reset")
async def copilot_reset(request: Request, db=Depends(get_db)):
    """Forget this conversation. Stored tasks are untouched."""
    owner_email = _copilot_owner_email(request)
    return JSONResponse(content={"ok": True, "cleared": copilot_memory.clear_history(db, "copilot", owner_email)})


# --- Training deck downloads (PPTX served from generated_documents) ----------
@app.get("/docs/download/{doc_id}", response_class=Response)
async def docs_download(doc_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM generated_documents WHERE id = %s;", (doc_id,))
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")
    media = (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        if row["file_type"] == "pptx"
        else ("application/pdf" if row["file_type"] == "pdf" else "application/octet-stream")
    )
    headers = {"Content-Disposition": f"attachment; filename=\"{row['file_name']}\""}
    return Response(content=bytes(row["file_data"]), media_type=media, headers=headers)


@app.post("/api/training/deck")
async def training_deck_build(request: Request, kind: str = Form("worker"), db=Depends(get_db)):
    require_admin(request)
    kind = "construction" if kind in ("construction", "construction-osha", "osha") else kind
    if kind not in ("worker", "construction"):
        return JSONResponse({"ok": False, "error": f"Unknown deck kind: {kind}"})
    try:
        data = training_service.build_deck(kind)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Could not build deck: {e}"})
    label = (
        "Crew Orientation + OSHA-10 Baseline" if kind == "construction"
        else "Worker Orientation"
    )
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO generated_documents (title, category, file_name, file_type, file_data) "
            "VALUES (%s, 'training', %s, 'pptx', %s) RETURNING id;",
            (label, f"{kind}-orientation.pptx", data),
        )
        doc_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse({"ok": True, "doc_id": doc_id, "download_url": f"/docs/download/{doc_id}"})


@app.post("/api/training/quiz")
async def training_quiz_submit(request: Request, worker_name: str = Form(...), email: str = Form(""), answers: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    try:
        parsed = json.loads(answers)
        if not isinstance(parsed, list):
            raise ValueError("expected a list of answers")
        answers_parsed = parsed
    except Exception:
        return JSONResponse({"ok": False, "error": "Answers came through malformed."})
    try:
        grade = training_service.grade_osha_quiz(answers_parsed)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)})
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_quiz_results (worker_id, worker_name, email, score, total, passed, answers_json) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id;",
            (None, worker_name, email, grade["correct"], grade["total"], grade["passed"], answers),
        )
        result_id = cur.fetchone()["id"]
        db.commit()
    return JSONResponse({"ok": True, "correct": grade["correct"], "total": grade["total"],
                         "percent": grade["percent"], "passed": grade["passed"],
                         "missed_topics": grade.get("missed_topics", []), "result_id": result_id})


# --- Appearance / theme -----------------------------------------------------
@app.get("/appearance", response_class=HTMLResponse)
async def appearance_page(request: Request):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    state = site_theme.state(force=True)
    schedule = []
    for skin, (sm, sd), (em, ed) in site_theme.SEASON_SCHEDULE:
        label = site_theme.PRESETS.get(skin, {}).get("label", skin)
        schedule.append((label, f"{sm}/{sd}", f"{em}/{ed}"))
    return templates.TemplateResponse(request=request, name="appearance.html", context={
        "user": {"email": user_email},
        "state": state,
        "presets": site_theme.PRESETS,
        "schedule": schedule,
        "fonts": [(k, site_theme.FONT_LABELS.get(k, k)) for k in site_theme.FONTS],
        "font_labels": site_theme.FONT_LABELS,
    })


@app.post("/appearance", response_class=HTMLResponse)
async def appearance_save(
    request: Request,
    auto: str = Form(""),
    skin: str = Form("default"),
    bg: str = Form(""),
    accent: str = Form(""),
    font: str = Form(""),
    emoji: str = Form(""),
    decor: str = Form(""),
    db=Depends(get_db),
):
    require_admin(request)
    # Unticked checkboxes post nothing, so "" means off for both switches.
    site_theme.save_theme(
        db,
        skin=skin, bg=bg or None, accent=accent or None,
        font=font or None, emoji=emoji,
        auto=(auto.lower() in ("on", "true", "1", "yes")),
        decor=(decor.lower() in ("on", "true", "1", "yes")),
    )
    return RedirectResponse(url="/appearance?saved=1", status_code=status.HTTP_303_SEE_OTHER)
