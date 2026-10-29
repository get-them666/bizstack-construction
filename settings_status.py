"""Status snapshot for the owner's Settings page.

Everything here is read-only and cheap except the OpenAI probe, which makes
one small authenticated request and caches the answer for five minutes. It
exists because on 2026-10-28 the OpenAI key was rejected and the phone line,
site chat and Copilot all went dark with no visible signal anywhere an
owner would look. Settings is where an owner looks.

Flags are parsed explicitly: Railway stores an unset toggle as the string
"false", which is truthy, so bare os.getenv() truthiness is wrong here.
"""
from __future__ import annotations

import os
import time
import urllib.error
import urllib.parse
import urllib.request

_ON = ("1", "true", "yes", "on")
_OFF = ("0", "false", "no", "off", "")


def flag_on(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _ON:
        return True
    if raw in _OFF:
        return default if raw == "" else False
    return default


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


# --- OpenAI --------------------------------------------------------------------

_OPENAI_CACHE: dict = {"at": 0.0, "result": None}
OPENAI_PROBE_TTL = 300


def openai_health(force: bool = False) -> dict:
    """{configured, ok, detail, model, checked_at} for the shared OpenAI key."""
    now = time.time()
    if not force and _OPENAI_CACHE["result"] and now - _OPENAI_CACHE["at"] < OPENAI_PROBE_TTL:
        return _OPENAI_CACHE["result"]
    key = env("OPENAI_API_KEY")
    model = env("COPILOT_MODEL") or env("OPENAI_MODEL", "gpt-4o-mini")
    result = {"configured": bool(key), "ok": False, "detail": "", "model": model,
              "checked_at": time.strftime("%H:%M")}
    if not key:
        result["detail"] = "OPENAI_API_KEY is not set"
    else:
        req = urllib.request.Request(
            f"https://api.openai.com/v1/models/{urllib.parse.quote(model)}",
            headers={"Authorization": f"Bearer {key}"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                result["ok"] = 200 <= resp.status < 300
                result["detail"] = f"key accepted, {model} available"
        except urllib.error.HTTPError as e:
            if e.code == 401:
                result["detail"] = "key rejected (401) — rotate OPENAI_API_KEY in Railway; phone bot, site chat and Copilot are down"
            elif e.code == 404:
                # Auth passed (a bad key is a 401 first), the model name did not.
                result["detail"] = f"key accepted, but model {model!r} was not found — check COPILOT_MODEL"
            elif e.code == 429:
                result["detail"] = "rate limited or out of credit (429) — check billing"
            else:
                result["detail"] = f"HTTP {e.code} from OpenAI"
        except Exception as e:
            result["detail"] = f"could not reach OpenAI: {type(e).__name__}"
    _OPENAI_CACHE["at"] = now
    _OPENAI_CACHE["result"] = result
    return result


# --- the rest ------------------------------------------------------------------

def background_jobs() -> list[dict]:
    """Each daemon loop, whether its kill switch is set, and its cadence."""
    return [
        {"name": "Permit importer", "off": flag_on("DISABLE_PERMIT_IMPORT"),
         "cadence": f"every {env('PERMIT_SCAN_HOURS', '24')} h", "switch": "DISABLE_PERMIT_IMPORT"},
        {"name": "Lead-source scan", "off": flag_on("DISABLE_LEAD_SCAN"),
         "cadence": f"every {env('LEAD_SCAN_INTERVAL_HOURS', '6')} h", "switch": "DISABLE_LEAD_SCAN"},
        {"name": "Lead enrichment autopilot", "off": flag_on("DISABLE_LEAD_ENRICH"),
         "cadence": f"daily, up to {env('LEAD_ENRICH_MAX', '10')} leads", "switch": "DISABLE_LEAD_ENRICH"},
        {"name": "Lender outreach cadence", "off": flag_on("DISABLE_LOAN_OUTREACH"),
         "cadence": "hourly", "switch": "DISABLE_LOAN_OUTREACH"},
        {"name": "Gmail inbound poll", "off": env("GMAIL_INBOUND", "on").lower() in ("off", "0", "false", "no"),
         "cadence": f"every {env('GMAIL_INBOUND_INTERVAL_SECONDS', '900')} s", "switch": "GMAIL_INBOUND"},
    ]


def outbound_guardrails() -> list[dict]:
    """The auto_reply limits, as the owner should read them (display only)."""
    review = not flag_on("LEAD_AUTO_SEND")
    return [
        {"name": "Lead auto-send", "value": "review mode — drafts wait for you" if review else "ON — first touches send automatically",
         "var": "LEAD_AUTO_SEND"},
        {"name": "Lead emails per day", "value": env("LEAD_EMAIL_DAILY_CAP", "50"), "var": "LEAD_EMAIL_DAILY_CAP"},
        {"name": "All email per day", "value": env("EMAIL_DAILY_CAP", "400"), "var": "EMAIL_DAILY_CAP"},
        {"name": "Texts per day", "value": env("TEXT_DAILY_CAP", "50"), "var": "TEXT_DAILY_CAP"},
        {"name": "Touches per lead", "value": f"{env('LEAD_MAX_TOUCHES', '2')} (then {env('LEAD_RETOUCH_DAYS', '5')} days before a retouch)",
         "var": "LEAD_MAX_TOUCHES"},
        {"name": "Suppression after opt-out", "value": f"{env('LEAD_SUPPRESSION_DAYS', '3650')} days", "var": "LEAD_SUPPRESSION_DAYS"},
    ]


def email_status(mail_status: dict | None) -> dict:
    transport = env("EMAIL_TRANSPORT", "gmail").lower() or "gmail"
    out = {
        "transport": transport,
        "from_addr": env("SMTP_FROM") or env("SMTP_USER"),
        "from_name": env("SMTP_NAME"),
        "notify_to": env("NOTIFY_EMAIL") or env("ADMIN_EMAIL"),
        "legacy_warning": transport == "legacy",
        "ok": None, "detail": "", "checked_at": "",
    }
    if mail_status:
        out.update({"ok": bool(mail_status.get("ok")), "detail": mail_status.get("detail", ""),
                    "checked_at": mail_status.get("checked_at", "")})
    return out


def phone_status(vapi_configured: bool, signalwire_configured: bool) -> dict:
    return {
        "business_line": env("COMPANY_PHONE", "+1 (757) 908-7121"),
        "vapi_ok": vapi_configured,
        "vapi_assistant": env("VAPI_ASSISTANT_ID")[:8] + "…" if env("VAPI_ASSISTANT_ID") else "",
        "sms_ok": signalwire_configured,
        "sms_number": env("SIGNALWIRE_PHONE_NUMBER") or env("SIGNALWIRE_PHONE"),
    }


def payments_status(stripe_configured: bool, deposit_cents: int) -> dict:
    key = env("STRIPE_SECRET_KEY")
    return {
        "ok": stripe_configured,
        "mode": "live" if key.startswith("sk_live_") else ("test" if key.startswith("sk_test_") else "unknown"),
        "deposit_default": f"${deposit_cents / 100:,.0f}",
        "webhook_ok": bool(env("STRIPE_WEBHOOK_SECRET").startswith("whsec_")),
    }
