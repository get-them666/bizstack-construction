"""Buildstack Construction Co. — FastAPI web app.

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
import json
import uuid
import hmac
import hashlib
import secrets
import threading
import time
import urllib.parse
from pathlib import Path
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import asyncio
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, UploadFile, Response, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosmtplib
from email.message import EmailMessage

from con_ai_agent import BusinessAIAgent
from loan_outreach import start_outreach_tasks
from stripe_service import StripeService, default_deposit_cents
from signalwire_service import SignalWireService
import property_service
import estimating_service
import auto_reply
import permit_service
import site_theme
import auth_service
import construction_radar
import materials_service
import training_service

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/buildstack")
templates = Jinja2Templates(directory="templates/construction")
stripe_svc = StripeService()
signalwire = SignalWireService()
APP_TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "America/New_York"))

LEAD_STATUSES = ["new", "contacted", "quoted", "deposit", "in_progress", "completed", "lost"]
STATUS_LABELS = {
    "new": "New", "contacted": "Contacted", "quoted": "Quoted", "deposit": "Deposit paid",
    "in_progress": "In progress", "completed": "Completed", "lost": "Lost",
}


# --- Company identity (env-driven so both brands stay configurable) ---------
def company() -> dict:
    return {
        "name": os.getenv("COMPANY_NAME", "Buildstack Construction Co."),
        "phone": os.getenv("COMPANY_PHONE", "+1 (757) 846-9275"),
        "phone_e164": os.getenv("SIGNALWIRE_PHONE", "+17578469275"),
        "email": os.getenv("COMPANY_EMAIL", "hello@bizstackperks.com"),
        "domain": os.getenv("COMPANY_DOMAIN", "construction.bizstackperks.com"),
        "license": os.getenv("CONTRACTOR_LICENSE", ""),
        "service_area": os.getenv(
            "SERVICE_AREA",
            "Williamsburg–Hampton Roads, VA · Currituck County & Elizabeth City, NC",
        ),
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
                    status VARCHAR(50) DEFAULT 'new',
                    deposit_status VARCHAR(50) DEFAULT 'none',
                    deposit_cents INTEGER,
                    stripe_session_id VARCHAR(255),
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_status VARCHAR(50) DEFAULT 'none';")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS deposit_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS stripe_session_id VARCHAR(255);")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS notes TEXT;")
                cur.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY,
                    lead_id INTEGER REFERENCES leads(id),
                    stripe_session_id VARCHAR(255),
                    stripe_payment_intent_id VARCHAR(255),
                    amount_cents INTEGER NOT NULL,
                    currency VARCHAR(10) DEFAULT 'usd',
                    status VARCHAR(50) NOT NULL DEFAULT 'paid',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
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
                cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    password_hash VARCHAR(255) NOT NULL,
                    role VARCHAR(50) DEFAULT 'admin',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
                """)
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS property_sqft INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_low_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_high_cents INTEGER;")
                cur.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS estimate_json TEXT;")
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

    yield


app = FastAPI(title="Buildstack Construction Co.", lifespan=lifecycle)

os.makedirs("static", exist_ok=True)
os.makedirs("uploads", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

WORKER_COOKIE = "worker_session"


# --- DB + auth plumbing -----------------------------------------------------
def get_db():
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


def _map_embed(address: str | None) -> str:
    return f"https://maps.google.com/maps?q={urllib.parse.quote_plus(address or '')}&output=embed"


def _map_directions(address: str | None) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={urllib.parse.quote_plus(address or '')}"


def _template_actor(request: Request):
    return current_actor(request)


def _template_features(request: Request):
    return {}


templates.env.globals["current_actor"] = _template_actor
templates.env.globals["current_features"] = _template_features
templates.env.globals["site_theme_state"] = lambda: site_theme.state()
templates.env.globals["company"] = company
templates.env.globals["map_embed"] = _map_embed
templates.env.globals["map_directions"] = _map_directions


def _secure_cookies() -> bool:
    return os.getenv("COOKIE_SECURE", "false").lower() == "true"


def _login_redirect(role: str) -> str:
    return {"admin": "/dashboard"}.get(role or "", "/")


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


# --- Crew feature toggles (namespaced away from Broom's perms_worker) -------
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
    if ident.lower() == admin_email.lower() and secret == admin_password:
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
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, source, status, company) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ai-assistant', 'new', 'construction') RETURNING id;",
                (name, phone, email, project_type, address, budget, timeline, notes),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "lead_id": lead_id, "message": f"Lead saved for {name}."}

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

        try:
            cfg = documents_service.smtp_config_from_env()
            if not documents_service.smtp_configured(cfg):
                return {"ok": False, "error": "SMTP not configured."}
            auto_reply.run_coro(documents_service.send_email(cfg, to, subject, body))
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) "
                    "VALUES ('outbound', 'email', 'system', %s, %s);",
                    (to, body),
                )
                db.commit()
            return {"ok": True, "sent_to": to}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def make_outbound_call(to, notes=""):
        digits = "".join(ch for ch in str(to or "") if ch.isdigit())
        if len(digits) < 10:
            return {"ok": False, "error": "I need a valid 10-digit phone number."}
        if not signalwire.is_configured():
            return {"ok": False, "error": "SignalWire is not configured."}
        e164 = ("+1" + digits) if not digits.startswith("1") else ("+" + digits)
        swml_base = (os.getenv("APP_BASE_URL", "") or f"https://{company()['domain']}").rstrip("/")
        swml_url = f"{swml_base}/comms/outbound-voice.swml"
        try:
            sid = signalwire.create_ai_outbound_call(e164, swml_url)
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

    def sister_business_summary():
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'broom';")
            leads = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM bookings;")
            bookings = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM workers WHERE is_active = TRUE;")
            workers = cur.fetchone()["c"]
            cur.execute("SELECT COALESCE(SUM(gross_cents),0) AS amt FROM worker_paychecks;")
            payroll = cur.fetchone()["amt"]
        return {
            "ok": True,
            "company": "Broom Service (bizstackperks.com)",
            "leads": leads,
            "bookings": bookings,
            "active_workers": workers,
            "payroll_paid_total_cents": payroll,
        }

    def run_site_health_check():
        site = f"https://{company()['domain']}"
        checks = {"construction_site": True, "broom_site": True, "sms": True, "stripe": True, "database": True}
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
                "broom": "https://bizstackperks.com ✓",
            },
            "checks": checks,
        }

    def generate_training_deck(kind="worker"):
        kind = training_service.CONSTRUCTION_DECK_KINDS.get(kind, kind)
        if kind not in ("worker", "host", "construction",
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
            "host": "Host & Lead Onboarding",
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

    return {
        "register_lead": register_lead,
        "lookup_leads": lookup_leads,
        "get_business_summary": get_business_summary,
        "list_leads": list_leads,
        "update_lead_status": update_lead_status,
        "send_sms_message": send_sms_message,
        "send_email_message": send_email_message,
        "create_deposit_link": create_deposit_link,
        "lookup_permits": lookup_permits,
        "list_crew": list_crew,
        "lookup_crew_timesheets": lookup_crew_timesheets,
        "get_payroll_summary": get_payroll_summary,
        "run_payroll": run_payroll,
        "get_accounting_summary": get_accounting_summary,
        "estimate_materials": estimate_materials,
        "get_material_price": get_material_price,
        "sister_business_summary": sister_business_summary,
        "make_outbound_call": make_outbound_call,
        "run_site_health_check": run_site_health_check,
        "generate_training_deck": generate_training_deck,
        "grade_training_quiz": grade_training_quiz,
        "training_status": training_status,
        "remind_crew_training": remind_crew_training,
    }


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


@app.get("/health")
async def health_check():
    return {"status": "ok"}


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
    pages = ["/", "/services", "/portfolio", "/about", "/quote", "/contact", "/legal"]
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
    db=Depends(get_db),
):
    src = (source or "").strip().lower() or "website"
    with db.cursor() as cur:
        cur.execute(
"INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, source, status, company) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'new', 'construction') RETURNING id;",
            (name, phone, email, project_type, address, budget, timeline, description, src),
        )
        lead_id = cur.fetchone()["id"]
        db.commit()
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
    return JSONResponse(content={"status": "success", "lead_id": lead_id})


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


# --- Admin: dashboard + leads ----------------------------------------------
@app.get("/dashboard", response_class=HTMLResponse)
async def read_dashboard(request: Request, db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE company = 'construction';")
        total_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status = 'new' AND company = 'construction';")
        new_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status IN ('quoted','deposit','in_progress') AND company = 'construction';")
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments WHERE company = 'construction';")
        pay = cur.fetchone()
        cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 8;")
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
async def leads_page(request: Request, status_filter: str = "", q: str = "", db=Depends(get_db)):
    is_authed, user_email = require_auth(request)
    if not is_authed:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    where, params = [], []
    if status_filter in LEAD_STATUSES:
        where.append("status = %s")
        params.append(status_filter)
    if q.strip():
        where.append("(name ILIKE %s OR phone ILIKE %s OR email ILIKE %s OR address ILIKE %s)")
        like = f"%{q.strip()}%"
        params += [like, like, like, like]
    where.append("company = 'construction'")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM leads {clause} ORDER BY created_at DESC LIMIT 500;", tuple(params))
        leads = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS c FROM leads WHERE company = 'construction' GROUP BY status;")
        counts = {r["status"]: r["c"] for r in cur.fetchall()}

    return templates.TemplateResponse(request=request, name="leads.html", context={
        "user": {"email": user_email},
        "leads": leads,
        "statuses": LEAD_STATUSES,
        "status_labels": STATUS_LABELS,
        "counts": counts,
        "active_status": status_filter,
        "q": q,
        "default_deposit": default_deposit_cents() / 100,
    })


@app.post("/api/leads/{lead_id}/status")
async def update_lead_status_route(lead_id: int, request: Request, status_val: str = Form("new"), db=Depends(get_db)):
    require_admin(request)
    if status_val not in LEAD_STATUSES:
        raise HTTPException(status_code=400, detail="Unknown status")
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (status_val, lead_id))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/leads", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/leads/{lead_id}/notes")
async def update_lead_notes(lead_id: int, request: Request, notes: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE leads SET notes = %s WHERE id = %s;", (notes, lead_id))
        db.commit()
    return JSONResponse(content={"status": "success"})


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

    with db.cursor() as cur:
        cur.execute("UPDATE leads SET deposit_status = 'pending', deposit_cents = %s WHERE id = %s;", (amount_cents, lead_id))
        db.commit()
    try:
        url = stripe_svc.create_deposit_session(
            lead_id=lead["id"],
            customer_name=lead["name"],
            customer_email=lead.get("email") or "",
            project_type=lead.get("project_type") or "",
            amount_cents=amount_cents,
            base_url=f"https://{company()['domain']}",
        )
        return JSONResponse(content={"url": url, "amount_cents": amount_cents})
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


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

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
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
                    "VALUES (%s, %s, %s, %s, %s, 'construction');",
                    (int(lead_id), session.get("id"), payment_intent, amount_total, currency),
                )
                db.commit()

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

    agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc))
    ai_reply = agent.process_inbound_text(f"Inbound SMS from {From}: {Body}")

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
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice-transcription', %s, 'system', %s);", (From, TranscriptionText))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'sms', 'system', %s, %s);", (From, ai_reply))
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
            swml_url = (os.getenv("APP_BASE_URL", "") or f"https://{company()['domain']}").rstrip("/") + "/comms/outbound-voice.swml"
            try:
                sid = signalwire.create_ai_outbound_call(to, swml_url)
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
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice-transcription', %s, 'system', %s);", (sid, text))
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
        greeting = (
            f"Hi {lead['name'].split()[0]}, this is the assistant from {company()['name']} calling about your "
            f"{lead.get('project_type') or 'project'} request. Quick one — are you still interested in a free estimate?"
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

HOW WE WORK
- Every project starts with a free on-site estimate. We do not quote final prices over the phone.
- We give a clear written scope, a fixed price (not open-ended time and materials), and a schedule.

WHAT TO DO ON A CALL
1. Find out what the caller needs done and where the property is.
2. Collect their name and best phone number.
3. Save it as a lead with the register_lead tool, including project type, address, and any budget or timeline they mention.
4. Tell them an estimator will call back to schedule a free walkthrough. Never promise a specific start date or final price.

GENERAL
- Point callers to https://{company()['domain']} or the free-estimate form.
- Never expose internal data, credentials, or secrets. Never invent prices or schedules.
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
                                    "function": "sister_business_summary",
                                    "description": "Describe the sister company Broom Service (short-term rental cleaning). Use when asked about what our sister company does.",
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


VOICE_ALLOWED_TOOLS = {
    "register_lead",
    "lookup_leads",
    "send_sms_message",
    "send_email_message",
    "estimate_materials",
    "get_material_price",
    "create_deposit_link",
    "list_leads",
    "update_lead_status",
    "lookup_permits",
    "list_crew",
    "lookup_crew_timesheets",
    "get_accounting_summary",
    "get_business_summary",
    "sister_business_summary",
    "make_outbound_call",
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
    if name == "sister_business_summary":
        if result.get("ok"):
            return str(result.get("message") or "Broom Service keeps short-term rentals stay-ready for guests.")
        return str(result.get("error") or "Couldn't pull that right now.")
    if name == "make_outbound_call":
        if result.get("ok"):
            return f"I'm calling {result.get('to')} now."
        return str(result.get("error") or "I couldn't place that call right now.")
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
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (name, phone, email, project_type, address, description, source, status, "
                "property_sqft, estimate_low_cents, estimate_high_cents, estimate_json, company) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'instant_quote', 'new', %s, %s, %s, %s, 'construction') RETURNING id;",
                (name, phone, email, project_type, address,
                 f"Instant ballpark {est['label']} on a {est['sqft']:,} sq ft property.",
                 est["sqft"] or None, est["low_cents"], est["high_cents"], json.dumps(est)),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
    except Exception as e:
        print(f"⚠️ instant quote lead save failed: {e}", flush=True)

    try:
        auto_reply.notify_owner_email(
            "construction", lead_id=lead_id, name=name, phone=phone, email=email,
            service=project_type, address=address, source="instant_quote",
        )
    except Exception as e:
        print(f"⚠️ instant quote owner notify failed: {e}", flush=True)

    try:
        auto_reply.auto_reply_to_lead(
            db, "construction", name=name, phone=phone, email=email,
            service=project_type, address=address,
            message=f"Instant ballpark {est['label']} on a {est['sqft']:,} sq ft property.",
            source="instant_quote", sqft=est.get("sqft"), lead_id=lead_id,
        )
    except Exception as e:
        print(f"⚠️ instant quote auto-reply failed: {e}", flush=True)

    if lead_id:
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
    db=Depends(get_db),
):
    name = (full_name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip()
    if not name or not phone:
        return RedirectResponse(url="/get-started?error=Please+add+your+name+and+phone", status_code=status.HTTP_303_SEE_OTHER)
    if not email:
        return RedirectResponse(url="/get-started?error=Please+add+your+email+%E2%80%94+we%27ll+send+your+quote+there", status_code=status.HTTP_303_SEE_OTHER)

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
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, "
                "source, status, referral_code, partner_id, campaign, company) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'acquisition', 'new', %s, %s, 'get-started', 'construction') RETURNING id;",
                (name, phone, (email or "").strip(), project_type, (address or "").strip(),
                 (budget or "").strip(), (timeline or "").strip(), (message or "").strip()[:2000],
                 ref_code or None, partner_id),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
    except Exception as e:
        print(f"⚠️ get-started lead save failed: {e}", flush=True)
        return RedirectResponse(url="/get-started?error=Something+went+wrong+—+please+try+again+or+call+us+directly", status_code=status.HTTP_303_SEE_OTHER)

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
            signalwire.send_sms(company()["phone_e164"], summary[:1500])
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
@app.post("/api/chat")
async def web_chat(message: str = Form(...), db=Depends(get_db)):
    agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc), subset="guest")
    reply = agent.process_inbound_text(f"Website chat: {message}")
    return JSONResponse(content={"reply": reply})


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
                "contractor_name, issue_date, estimated_value, source, is_demo) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'permits',%s) RETURNING id;",
                (p["permit_number"], p["property_address"], p["city"], p["state"], p["work_type"],
                 p["job_description"], p["contractor_name"], p["issue_date"], est_val,
                 p.get("_demo", False)),
            )
            cur.fetchone()
            added += 1
            if p.get("_demo") or not permit_service.wants_lead(p):
                continue
            raw = p["permit_number"] or p.get("_id") or p["property_address"]
            digest = hashlib.sha256(raw.encode()).hexdigest()
            listing = f"https://api.shovels.ai/v2/permits?id={urllib.parse.quote(p.get('_id') or '')}" if p.get("_id") else (p["permit_number"] or "")
            meta = json.dumps({
                "address": p["property_address"], "city": p["city"], "permit_number": p["permit_number"],
                "permit_id": p.get("_id", ""), "work_type": p["work_type"],
                "contractor_name": p["contractor_name"], "value": est_val, "source": "permits",
            })
            cur.execute(
                "INSERT INTO leads (name, email, listing_url, status, source, referral_code, campaign, analysis_json, company) "
                "VALUES (%s, %s, %s, 'new', %s, %s, 'permit-radar', %s, 'construction') RETURNING id;",
                (
                    f"Permit · {p['property_address'] or p['city'] or 'Hampton Roads'}",
                    f"con-permit-{digest[:15]}@lead.local",
                    listing,
                    "permit_finder",
                    p["permit_number"],
                    meta,
                ),
            )
            lead_id = cur.fetchone()["id"]
            created_leads.append({
                "lead_id": lead_id,
                "name": p["contractor_name"] or "Homeowner",
                "service": p["work_type"] or (p["job_description"] or "")[:80],
                "address": p["property_address"],
                "budget": f"${est_val:,.0f}" if est_val else "",
            })
        conn.commit()
    for item in created_leads:
        try:
            auto_reply.notify_owner_email(
                "construction", lead_id=item["lead_id"], name=item["name"], email="",
                service=item["service"], address=item["address"], budget=item["budget"],
                source="permit_finder",
                message="A new building permit signals a homeowner who will need a contractor.",
            )
        except Exception as exc:
            print(f"[permit-scan] notify failed for lead {item['lead_id']}: {exc}", flush=True)
        try:
            delay = float(os.getenv("PERMIT_NOTIFY_DELAY", "0.4") or 0.4)
        except (TypeError, ValueError):
            delay = 0.4
        if delay > 0:
            time.sleep(delay)
    return added, len(created_leads)


def run_permit_import():
    if not permit_service.is_configured():
        print("[permit-scan] SHOVELS_API_KEY not set — skipping auto-import", flush=True)
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
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            permits = permit_service.search_permits(days=days)
            added, leads = ingest_permits(conn, permits)
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
    require_admin(request)
    where, params = [], []
    if status_filter:
        where.append("status = %s")
        params.append(status_filter)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM job_leads {clause} ORDER BY found_at DESC LIMIT 500;", tuple(params))
        rows = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS c FROM job_leads GROUP BY status;")
        counts = {r["status"]: r["c"] for r in cur.fetchall()}
    return templates.TemplateResponse(request=request, name="job_leads.html", context={
        "user": {"email": require_auth(request)[1]},
        "leads": rows, "counts": counts, "active_status": status_filter,
        "shovels_configured": permit_service.is_configured(),
        "permit_status": _permit_status(),
    })


@app.post("/api/job-leads/refresh")
async def job_leads_refresh(request: Request, city: str = Form("Chesapeake"), state: str = Form("VA"), db=Depends(get_db)):
    require_admin(request)
    permits = permit_service.fetch_permits(city, state)
    added, leads = ingest_permits(db, permits)
    return RedirectResponse(url=f"/job-leads?ok={added} permits + {leads} leads", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/job-leads/{job_id}/convert")
async def job_lead_convert(job_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT * FROM job_leads WHERE id = %s;", (job_id,))
        job = cur.fetchone()
    if not job:
        raise HTTPException(status_code=404, detail="Job lead not found")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (name, phone, email, project_type, address, description, source, status, company) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'permit_finder', 'new', 'construction') RETURNING id;",
            (job["contractor_name"] or job["city"] or "Property owner",
             job["owner_contact"] or "", job["owner_contact"] or "",
             job["work_type"] or "Renovation",
             job["address"] or "",
             f"Permit #{job['permit_number'] or ''}: {job['job_description'] or ''} (est. value ${job['estimated_value'] or 0:,.0f})"),
        )
        lead_id = cur.fetchone()["id"]
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


# --- Projects (active jobs for the crew) ------------------------------------
PROJECT_STATUSES = ["planned", "active", "on_hold", "completed", "cancelled"]


@app.get("/projects", response_class=HTMLResponse)
async def projects_page(request: Request, status_filter: str = "", db=Depends(get_db)):
    require_admin(request)
    where, params = [], []
    if status_filter:
        where.append("p.status = %s")
        params.append(status_filter)
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
        cur.execute("SELECT id, name, phone, address, project_type, status FROM leads WHERE company = 'construction' ORDER BY created_at DESC LIMIT 25;")
        lead_rows = cur.fetchall()
    return templates.TemplateResponse(request=request, name="projects.html", context={
        "user": {"email": require_auth(request)[1]},
        "projects": projects, "crew": crew_rows, "leads": lead_rows,
        "statuses": PROJECT_STATUSES, "active_status": status_filter,
    })


@app.post("/api/projects")
async def project_create(request: Request, name: str = Form(""), lead_id: str = Form(""), address: str = Form(""),
                         summary: str = Form(""), db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        if lead_id:
            cur.execute("SELECT name, address, project_type FROM leads WHERE id = %s;", (int(lead_id),))
            lead = cur.fetchone()
            if lead and (not name or not address):
                name = name or f"{lead['project_type'] or 'Project'} — {lead['name']}"
                address = address or lead["address"] or ""
        cur.execute(
            "INSERT INTO projects (lead_id, name, address, summary, status) VALUES (%s,%s,%s,%s,'planned') RETURNING id;",
            (int(lead_id) if lead_id else None, name or "Untitled project", address, summary),
        )
        pid = cur.fetchone()["id"]
        db.commit()
    return RedirectResponse(url=f"/projects?ok=created+%23{pid}", status_code=status.HTTP_303_SEE_OTHER)


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
            "start_date=%s, end_date=%s, notes=%s WHERE id=%s;",
            (name, address, summary, status_val, start_date or None, end_date or None, notes, project_id),
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
    with open(os.path.join("uploads", fname), "wb") as fh:
        fh.write(raw)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO job_photos (project_id, crew_id, filename, caption, is_punchlist) VALUES (%s,%s,%s,%s,%s);",
            (project_id or None, worker.get("id"), fname, caption, punchlist.lower() in ("on", "true", "1")),
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
                "url": f"/uploads/{r['filename']}",
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
    with open(os.path.join("uploads", fname), "wb") as fh:
        fh.write(raw)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO job_photos (project_id, crew_id, filename, caption, is_punchlist) VALUES (%s,%s,%s,%s,%s);",
            (project_id or None, worker.get("id"), fname, caption, punchlist.lower() in ("on", "true", "1")),
        )
        db.commit()
    return JSONResponse(content={"status": "ok", "url": f"/uploads/{fname}"})


# --- Crew app: Messages with the office ---------------------------------------
@app.get("/api/crew/app/messages")
async def worker_app_messages(request: Request, after: int = 0, db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "messages")
    with db.cursor() as cur:
        cur.execute("""
            SELECT m.id, m.author_role, m.author_id, m.author_name, m.company, m.kind,
                   m.audience, m.scope, m.recipient_role, m.recipient_id, m.recipient_name,
                   m.body, m.created_at
            FROM messages m
            WHERE m.id > %s
              AND (m.company = 'construction'
                   OR m.author_id = %s
                   OR m.recipient_id = %s)
            ORDER BY m.id ASC LIMIT 500;
        """, (after, worker.get("id"), worker.get("id")))
        rows = cur.fetchall()
    msgs = [_crew_message_visible_and_serialize(m, worker) for m in rows]
    msgs = [m for m in msgs if m is not None]
    return JSONResponse(content={"messages": msgs})


def _crew_message_visible_and_serialize(m, worker):
    """Mirror Broom's _message_visible semantics for construction crew: a DM is
    shown if this crew member wrote it or it is addressed to them; office
    announcements show whenever the scope covers construction/all."""
    kind = m.get("kind") or "announcement"
    if kind == "dm":
        is_author = (m["author_role"] == "crew" and m["author_id"] == worker.get("id"))
        is_recipient = (m["recipient_id"] == worker.get("id"))
        if not (is_author or is_recipient):
            return None
    else:
        scope = m.get("scope") or "all"
        if scope not in ("all", "construction"):
            return None
    return {
        "id": m["id"], "kind": kind,
        "author": ("crew" if m["author_role"] == "crew" else "office"),
        "author_id": m["author_id"],
        "author_name": m["author_name"] or ("Office" if m["author_role"] == "admin" else "Crew"),
        "recipient_role": m["recipient_role"], "recipient_id": m["recipient_id"],
        "body": m["body"], "at": m["created_at"].isoformat(),
    }


@app.post("/api/crew/app/messages")
async def worker_app_messages_send(request: Request, body: str = Form(...), db=Depends(get_db)):
    worker = require_worker(request)
    _require_crew_feature(db, "messages")
    body = (body or "").strip()[:2000]
    if not body:
        return JSONResponse(content={"status": "error", "error": "Empty message"}, status_code=400)
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO messages (author_role, author_id, author_name, company, kind, audience, scope, recipient_role, body) "
            "VALUES ('crew', %s, %s, 'construction', 'dm', 'office', 'all', 'admin', %s) RETURNING id, created_at;",
            (worker.get("id"), worker.get("name", ""), body),
        )
        row = cur.fetchone()
        new_id = row["id"]
        db.commit()
    _broadcast_ws({
        "id": new_id, "kind": "dm", "author": "crew",
        "author_id": worker.get("id"), "author_name": worker.get("name", "Crew"),
        "recipient_role": "admin", "body": body, "at": row["created_at"].isoformat(),
    })
    return JSONResponse(content={"status": "ok", "id": new_id})


WS_CLIENTS: set = set()


@app.websocket("/ws/messages")
async def ws_messages(websocket: WebSocket):
    await websocket.accept()
    WS_CLIENTS.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        WS_CLIENTS.discard(websocket)


def _broadcast_ws(message: dict):
    for client in list(WS_CLIENTS):
        try:
            asyncio.create_task(client.send_json(message))
        except Exception:
            WS_CLIENTS.discard(client)


# --- Training: safety orientation (OSHA-10 baseline) + trade skills tests -------------
# Crew portal page and phone-app API. Content lives in training_service.py. Company
# safety orientation module covers OSHA implementing rules (29 CFR 1926) and site
# safety requirements. It is informational company training — NOT official OSHA-10/30
# certification, which only an authorized OSHA trainer can issue.
TRAINING_DISCLAIMER = ("This is Buildstack Construction company training covering OSHA "
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
    "host": "host-onboarding.pptx",
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
    return RedirectResponse(url=f"/payroll?period_start={ps}&period_end={pe}&ok=run+created+{jobs_done}",
                            status_code=status.HTTP_303_SEE_OTHER)


@app.post("/api/payroll/run/{run_id}/finalize")
async def payroll_run_finalize(run_id: int, request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("UPDATE payroll_runs SET status = 'closed' WHERE id = %s;", (run_id,))
        db.commit()
    return RedirectResponse(url=request.headers.get("referer") or "/payroll", status_code=status.HTTP_303_SEE_OTHER)


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
    return templates.TemplateResponse(request=request, name="copilot.html", context={"user": {"email": user_email}})


@app.post("/api/copilot")
async def copilot_chat(request: Request, message: str = Form(...), db=Depends(get_db)):
    require_admin(request)
    agent = BusinessAIAgent(knowledge_path="construction_knowledge.md", tool_handlers=build_tool_handlers(db, stripe_svc), subset="copilot")
    reply = agent.process_inbound_text(message)
    return JSONResponse(content={"reply": reply})


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
    if kind not in ("worker", "host", "construction"):
        return JSONResponse({"ok": False, "error": f"Unknown deck kind: {kind}"})
    try:
        data = training_service.build_deck(kind)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Could not build deck: {e}"})
    label = (
        "Crew Orientation + OSHA-10 Baseline" if kind == "construction"
        else ("Worker Orientation" if kind == "worker" else "Host & Lead Onboarding")
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
    return templates.TemplateResponse(request=request, name="appearance.html", context={
        "user": {"email": user_email},
        "state": state,
        "presets": site_theme.PRESETS,
        "fonts": list(site_theme.FONTS.keys()),
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
    db=Depends(get_db),
):
    require_admin(request)
    site_theme.save_theme(
        db,
        skin=skin, bg=bg or None, accent=accent or None,
        font=font or None, emoji=emoji, auto=(auto.lower() in ("on", "true", "1", "yes")),
    )
    return RedirectResponse(url="/appearance", status_code=status.HTTP_303_SEE_OTHER)
