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
import urllib.parse
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, Request, Form, UploadFile, Response, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import aiosmtplib
from email.message import EmailMessage

from ai_agent import BusinessAIAgent
from stripe_service import StripeService, default_deposit_cents
from signalwire_service import SignalWireService
import property_service
import estimating_service
import permit_service
import site_theme
import auth_service

db_url = os.getenv("DATABASE_URL", "postgresql://shaun:secret@localhost:5432/buildstack")
templates = Jinja2Templates(directory="templates")
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
        "email": os.getenv("COMPANY_EMAIL", "hello@buildstackconstruction.com"),
        "domain": os.getenv("COMPANY_DOMAIN", "buildstackconstruction.com"),
        "license": os.getenv("CONTRACTOR_LICENSE", ""),
        "service_area": os.getenv(
            "SERVICE_AREA",
            "Williamsburg–Hampton Roads, VA · Currituck County & Elizabeth City, NC",
        ),
        "founded": "2026",
    }


def _money(value):
    try:
        return f"${float(value):,.0f}"
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
            conn.commit()
        print("✅ Schema ready.")
    except Exception as e:
        print(f"⚠️ Database init skipped: {e}")
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
    return os.getenv("APP_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


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
    admin_email = os.getenv("ADMIN_EMAIL", "admin@buildstackconstruction.com")
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
        "from": os.getenv("SMTP_FROM", "noreply@buildstackconstruction.com"),
        "name": os.getenv("SMTP_NAME", company()["name"]),
        "tls": os.getenv("SMTP_TLS", "true").lower(),
    }


async def _deliver_otp(email: str, code: str, actor: dict):
    cfg = _smtp_cfg()
    if not cfg["host"]:
        print(f"[OTP] SMTP not configured — code for {email}: {code}")
        return False, "Email delivery is not configured yet."
    body = (
        f"Hi {actor.get('name') or 'there'},\n\n"
        f"Your {cfg['name']} verification code is:\n\n    {code}\n\n"
        f"It expires in 10 minutes. If you didn't try to sign in, you can ignore this email.\n\n"
        f"— {cfg['name']}"
    )
    msg = EmailMessage()
    msg["From"] = f"{cfg['name']} <{cfg['from']}>"
    msg["To"] = email
    msg["Subject"] = f"Your {cfg['name']} login code"
    msg.set_content(body)
    try:
        await aiosmtplib.send(
            msg,
            hostname=cfg["host"],
            port=cfg["port"],
            username=cfg["user"] or None,
            password=cfg["pass"] or None,
            start_tls=(cfg["tls"] == "true"),
            use_tls=(cfg["tls"] == "ssl"),
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
                "INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, source, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ai-assistant', 'new') RETURNING id;",
                (name, phone, email, project_type, address, budget, timeline, notes),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
        return {"ok": True, "lead_id": lead_id, "message": f"Lead saved for {name}."}

    def lookup_leads(phone):
        digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
        with db.cursor() as cur:
            cur.execute("SELECT id, name, project_type, status, deposit_status, created_at FROM leads ORDER BY created_at DESC LIMIT 200;")
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
            cur.execute("SELECT COUNT(*) AS c FROM leads;")
            total = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status NOT IN ('completed','lost');")
            open_leads = cur.fetchone()["c"]
            cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments;")
            pay = cur.fetchone()
        return {
            "ok": True,
            "total_leads": total,
            "open_leads": open_leads,
            "deposits_collected": pay["c"],
            "deposit_amount_cents": pay["amt"],
        }

    def list_leads(status_filter=""):
        with db.cursor() as cur:
            if status_filter:
                cur.execute(
                    "SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads WHERE status = %s ORDER BY created_at DESC LIMIT 100;",
                    (status_filter,),
                )
            else:
                cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads ORDER BY created_at DESC LIMIT 100;")
            rows = cur.fetchall()
        return {"ok": True, "leads": [
            {**{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}} for r in rows
        ]}

    def update_lead_status(lead_id, status_val):
        if status_val not in LEAD_STATUSES:
            return {"ok": False, "error": f"Unknown status: {status_val}"}
        with db.cursor() as cur:
            cur.execute("UPDATE leads SET status = %s WHERE id = %s;", (status_val, lead_id))
            db.commit()
        return {"ok": True, "lead_id": lead_id, "status": status_val}

    def send_sms_message(to, body):
        ok = signalwire.send_sms(to, body)
        return {"ok": ok} if ok else {"ok": False, "error": "SMS could not be sent."}

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
            )
            return {"ok": True, "url": url}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return {
        "register_lead": register_lead,
        "lookup_leads": lookup_leads,
        "get_business_summary": get_business_summary,
        "list_leads": list_leads,
        "update_lead_status": update_lead_status,
        "send_sms_message": send_sms_message,
        "create_deposit_link": create_deposit_link,
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
            "INSERT INTO leads (name, phone, email, project_type, address, budget, timeline, description, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'new') RETURNING id;",
            (name, phone, email, project_type, address, budget, timeline, description, src),
        )
        lead_id = cur.fetchone()["id"]
        db.commit()
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
        cur.execute("SELECT COUNT(*) AS c FROM leads;")
        total_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status = 'new';")
        new_leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM leads WHERE status IN ('quoted','deposit','in_progress');")
        active = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments;")
        pay = cur.fetchone()
        cur.execute("SELECT id, name, phone, project_type, status, deposit_status, created_at FROM leads ORDER BY created_at DESC LIMIT 8;")
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
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    with db.cursor() as cur:
        cur.execute(f"SELECT * FROM leads {clause} ORDER BY created_at DESC LIMIT 500;", tuple(params))
        leads = cur.fetchall()
        cur.execute("SELECT status, COUNT(*) AS c FROM leads GROUP BY status;")
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
               ORDER BY p.created_at DESC LIMIT 500;"""
        )
        payments = cur.fetchall()
        cur.execute("SELECT COUNT(*) AS c, COALESCE(SUM(amount_cents),0) AS amt FROM payments;")
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
                    "INSERT INTO payments (lead_id, stripe_session_id, stripe_payment_intent_id, amount_cents, currency) "
                    "VALUES (%s, %s, %s, %s, %s);",
                    (int(lead_id), session.get("id"), payment_intent, amount_total, currency),
                )
                db.commit()

    return Response(content='{"received": true}', media_type="application/json")


# --- Business API -----------------------------------------------------------
@app.get("/api/business/summary")
async def business_summary(request: Request, db=Depends(get_db)):
    require_admin(request)
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS c FROM leads;")
        leads = cur.fetchone()["c"]
        cur.execute("SELECT COUNT(*) AS c FROM payments;")
        deposits = cur.fetchone()["c"]
    return {"leads": leads, "deposits": deposits}


# --- SignalWire SMS webhook -------------------------------------------------
@app.post("/comms/sms-webhook")
async def inbound_sms_webhook(request: Request, db=Depends(get_db)):
    form = await request.form()
    From = form.get("From", "")
    Body = form.get("Body", "")

    agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc))
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
    <Say voice="alice">{greeting}</Say>
    <Record maxLength="30" action="/comms/voice-action" transcribe="true" transcribeCallback="/comms/voice-transcribe"/>
</Response>"""
    return Response(content=twiml_payload, media_type="application/xml")


@app.post("/comms/voice-action")
async def voice_action(request: Request, db=Depends(get_db)):
    ai_response = "Thanks for the details. Our team will follow up shortly to schedule your free estimate. Have a great day!"
    twiml_payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="alice">{ai_response}</Say>
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
            agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc))
            ai_reply = agent.process_inbound_text(f"Voice transcription from {From}: {TranscriptionText}")
        except Exception:
            ai_reply = "Thank you for your call. Our team will follow up shortly."

        with db.cursor() as cur:
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('inbound', 'voice-transcription', %s, 'system', %s);", (From, TranscriptionText))
            cur.execute("INSERT INTO comms_logs (direction, channel, sender, recipient, message_body) VALUES ('outbound', 'sms', 'system', %s, %s);", (From, ai_reply))
            db.commit()

    return Response(content="", status_code=204)


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


def _swaig_parameters(props: dict, required: list, notes: str = ""):
    return {
        "type": "object",
        "required": required,
        "properties": props,
        "$comment": notes,
        "additionalProperties": False,
    }


@app.api_route("/voice.swml", methods=["GET", "POST"])
@app.api_route("/voice-app.swml", methods=["GET", "POST"])
async def voice_swml():
    swml = {
        "version": "1.0.0",
        "sections": {
            "main": [
                {"answer": {}},
                {
                    "ai": {
                        "prompt": {"text": VOICE_AGENT_PROMPT},
                        "languages": [
                            {
                                "name": "English",
                                "code": "en-US",
                                "voice": "elevenlabs.rachel",
                                "speech_fillers": ["one moment please,", "hmm...", "let's see,"],
                            }
                        ],
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
                            ],
                        },
                    }
                },
            ]
        },
    }
    return JSONResponse(content=swml)


VOICE_ALLOWED_TOOLS = {"register_lead", "lookup_leads", "send_sms_message"}


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
                "property_sqft, estimate_low_cents, estimate_high_cents, estimate_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'instant_quote', 'new', %s, %s, %s, %s) RETURNING id;",
                (name, phone, email, project_type, address,
                 f"Instant ballpark {est['label']} on a {est['sqft']:,} sq ft property.",
                 est["sqft"] or None, est["low_cents"], est["high_cents"], json.dumps(est)),
            )
            lead_id = cur.fetchone()["id"]
            db.commit()
    except Exception as e:
        print(f"⚠️ instant quote lead save failed: {e}", flush=True)

    if phone and lead_id:
        try:
            signalwire.send_sms(phone, (
                f"Hi {name or 'there'}! Your instant ballpark from Buildstack Construction is "
                f"{_format_currency(est['low_cents'])}–{_format_currency(est['high_cents'])}. "
                "It's a range, not a bid — reply to book a free walkthrough, or call (757) 846-9275."
            ))
        except Exception as e:
            print(f"⚠️ instant quote SMS failed: {e}", flush=True)

    ctx.update({"quote": est, "lead_id": lead_id, "prop": prop})
    return templates.TemplateResponse(request=request, name="instant_quote.html", context=ctx)


# --- Public web chat (same AI agent as the phones) --------------------------
@app.post("/api/chat")
async def web_chat(message: str = Form(...), db=Depends(get_db)):
    agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc), subset="guest")
    reply = agent.process_inbound_text(f"Website chat: {message}")
    return JSONResponse(content={"reply": reply})


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
    })


@app.post("/api/job-leads/refresh")
async def job_leads_refresh(request: Request, city: str = Form("Williamsburg"), state: str = Form("VA"), db=Depends(get_db)):
    require_admin(request)
    permits = permit_service.fetch_permits(city, state)
    added = 0
    with db.cursor() as cur:
        for p in permits:
            cur.execute(
                "SELECT id FROM job_leads WHERE permit_number = %s AND address = %s;",
                (p["permit_number"], p["property_address"]),
            )
            if cur.fetchone():
                continue
            cur.execute(
                "INSERT INTO job_leads (permit_number, address, city, state, work_type, job_description, "
                "contractor_name, issue_date, estimated_value, source, is_demo) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'permits',%s) RETURNING id;",
                (p["permit_number"], p["property_address"], p["city"], p["state"], p["work_type"],
                 p["job_description"], p["contractor_name"], p["issue_date"], p["estimated_value"],
                 p.get("_demo", False)),
            )
            cur.fetchone()
            added += 1
        db.commit()
    return RedirectResponse(url=f"/job-leads?ok=added+{added}", status_code=status.HTTP_303_SEE_OTHER)


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
            "INSERT INTO leads (name, phone, email, project_type, address, description, source, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'permit_finder', 'new') RETURNING id;",
            (job["contractor_name"] or job["city"] or "Property owner",
             job["owner_contact"] or "", job["owner_contact"] or "",
             job["work_type"] or "Renovation",
             job["address"] or "",
             f"Permit #{job['permit_number'] or ''}: {job['job_description'] or ''} (est. value ${job['estimated_value'] or 0:,.0f})"),
        )
        lead_id = cur.fetchone()["id"]
        cur.execute("UPDATE job_leads SET status = 'contacted' WHERE id = %s;", (job_id,))
        db.commit()
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
        cur.execute("SELECT id, name, phone, address, project_type, status FROM leads ORDER BY created_at DESC LIMIT 25;")
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
            "INSERT INTO payments (lead_id, stripe_payment_intent_id, amount_cents, currency, status) "
            "VALUES (NULL, %s, %s, 'usd', 'paid');",
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
    agent = BusinessAIAgent(tool_handlers=build_tool_handlers(db, stripe_svc), subset="copilot")
    reply = agent.process_inbound_text(message)
    return JSONResponse(content={"reply": reply})


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
