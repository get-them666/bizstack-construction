"""Daily autopilot: address-only leads get a contact and a staged first draft.

WHY THIS EXISTS
---------------
Every step to turn an address-only lead into outreachable work already existed
-- free parcel lookup (skiptrace_service), Skip Sherpa for phone/email
(data_enrichment), conservative write-back (data_enrichment.merge), and a
review-gated draft in leads.draft_reply that the owner fires from the leads
page (auto_reply.fire_lead_draft). What never existed was anything that ran
the sequence without being asked. 1,487 leads sat unreachable because the
pipeline only moved when somebody clicked.

This thread moves it on a schedule, hard-capped:

  LEAD_ENRICH_MAX     leads examined per run (default 10). Each examined lead
                      can cost AT MOST ONE Skip Sherpa credit, so this is also
                      the daily credit ceiling.
  DISABLE_LEAD_ENRICH kill switch, parsed explicitly (see _env_flag).
  Runs at most once per calendar day, recorded in app_settings, so redeploys
  and crash restarts cannot re-spend the cap within one day.

NOTHING HERE SENDS. A gained contact stages a draft into leads.draft_reply and
stops. The owner reviews and sends; auto_reply's caps and contact policy
re-check at send time. This module only researches and stages.

Cost order is inherited from data_enrichment.lookup_address: free parcel data
first, Skip Sherpa last and only when a cheaper step found no channel, and
never without a full ZIP.
"""

import os
import threading
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row

import data_enrichment

# The standard first-touch note. Single source of truth: the Copilot's
# draft_lead_email handler builds its body from this function too, so the
# wording an owner sees staged by hand and staged by this loop is one copy.
SUBJECT = "Hello from Buildstack Construction"


def first_touch_draft(name, address) -> tuple:
    first = (str(name or "").strip() or "there").split()[0]
    addr = str(address or "").strip()
    body = (
        f"Hi {first},\n\n"
        f"I hope this message finds you well! I'm reaching out from Buildstack "
        f"Construction, as we noticed your property at {addr}. "
        f"If you're considering any renovations or improvements, we'd love to help "
        f"you with your project.\n\n"
        f"Feel free to reply to this email or give us a call at "
        f"+1 (757) 908-7121 to discuss your ideas!\n\n"
        f"Best regards,\n"
        f"Buildstack Construction"
    )
    return SUBJECT, body


def _env_flag(name: str) -> bool:
    """True when the flag says OFF. Explicit on purpose.

    Railway stores an unset flag as the STRING "false", which bare os.getenv
    truthiness reads as ON and silently disables everything. Only an explicit
    1/true/yes/on disables the loop.
    """
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _daily_max() -> int:
    try:
        return max(0, int(os.getenv("LEAD_ENRICH_MAX") or 10))
    except (TypeError, ValueError):
        return 10


def select_batch(db, limit: int) -> list:
    """Unreachable leads with a real address, freshest first.

    Unreachable = no phone AND no real email (empty or the @lead.local
    placeholder a permit import leaves behind). Dead statuses are excluded so
    the loop never spends a credit on a closed job. Newest first: fresh
    permits are the hot ones, and the aged backlog stays put rather than
    consuming the whole daily cap forever.
    """
    if limit <= 0:
        return []
    dead = ("cancelled", "canceled", "closed", "lost", "won", "archived")
    sql = (
        "SELECT id, name, email, phone, address, source, status, draft_reply "
        "FROM leads "
        "WHERE COALESCE(address, '') <> '' "
        "  AND (COALESCE(TRIM(email), '') = '' OR LOWER(TRIM(email)) LIKE '%%@lead.local') "
        "  AND COALESCE(TRIM(phone), '') = '' "
        "  AND LOWER(COALESCE(status, 'new')) NOT IN ("
        + ",".join(["%s"] * len(dead)) + ") "
        "ORDER BY id DESC LIMIT %s"
    )
    with db.cursor() as cur:
        cur.execute(sql, (*dead, limit))
        return cur.fetchall()


def _already_ran_today(db) -> bool:
    with db.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS app_settings ("
                    "key TEXT PRIMARY KEY, value TEXT, "
                    "updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP);")
        cur.execute("SELECT value FROM app_settings WHERE key = 'lead_enrich_last_run';")
        row = cur.fetchone()
    today = date.today().isoformat()
    return bool(row) and (row["value"] if isinstance(row, dict) else row[0]) == today


def _mark_ran(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_settings (key, value) VALUES ('lead_enrich_last_run', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
            "updated_at = CURRENT_TIMESTAMP;",
            (date.today().isoformat(),),
        )
    db.commit()


def run_once(db=None) -> dict:
    """One capped pass. Never raises; returns a summary dict."""
    summary = {"examined": 0, "enriched": 0, "name_only": 0, "unchanged": 0,
               "drafts": 0, "sherpa_lookups": 0, "skipped": "", "steps": []}
    if _env_flag("DISABLE_LEAD_ENRICH"):
        summary["skipped"] = "DISABLE_LEAD_ENRICH is set"
        return summary
    cap = _daily_max()
    if cap == 0:
        summary["skipped"] = "LEAD_ENRICH_MAX is 0"
        return summary

    own_connection = db is None
    if own_connection:
        db_url = (os.getenv("DATABASE_URL") or "").strip()
        if not db_url:
            summary["skipped"] = "DATABASE_URL is not set"
            return summary
        db = psycopg.connect(db_url, row_factory=dict_row)

    try:
        if _already_ran_today(db):
            summary["skipped"] = "already ran today"
            return summary
        # Marked BEFORE spending, not after: a crash mid-run must not let the
        # next restart re-spend the day's credits.
        _mark_ran(db)

        for lead in select_batch(db, cap):
            if data_enrichment.classify_reachable(lead):
                continue
            summary["examined"] += 1
            found = data_enrichment.lookup_address(db, lead.get("address"), None)
            if any(s.startswith("skip_sherpa:") for s in found.get("steps", [])):
                summary["sherpa_lookups"] += 1
            merged = data_enrichment.merge(lead, found)
            if merged["changed"]:
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET name = %s, email = %s, phone = %s WHERE id = %s;",
                        (merged["name"][:255], merged["email"][:255],
                         merged["phone"][:50], lead["id"]),
                    )
                db.commit()
                if merged["gained_contact"]:
                    summary["enriched"] += 1
                else:
                    summary["name_only"] += 1
            else:
                summary["unchanged"] += 1

            email = (merged.get("email") or "").strip()
            has_draft = bool(str(lead.get("draft_reply") or "").strip())
            # Phone-only gains do not stage: email is the channel that sends
            # today, and a draft aimed at an unsendable channel is noise.
            if merged["gained_contact"] and email and not email.lower().endswith("@lead.local") \
                    and not has_draft:
                subject, body = first_touch_draft(lead.get("name"), lead.get("address"))
                with db.cursor() as cur:
                    cur.execute(
                        "UPDATE leads SET draft_reply = %s "
                        "WHERE id = %s AND (draft_reply IS NULL OR TRIM(draft_reply) = '');",
                        (f"{subject}\n{body}", lead["id"]),
                    )
                    staged = cur.rowcount
                db.commit()
                if staged:
                    summary["drafts"] += 1

            summary["steps"].append(
                f"#{lead['id']} {str(lead.get('address') or '')[:34]} -> "
                + "; ".join(found.get("steps", []))
            )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        summary["skipped"] = f"run failed: {type(exc).__name__}: {exc}"
    finally:
        if own_connection:
            try:
                db.close()
            except Exception:
                pass
    return summary


def start(interval_seconds: int = 3600) -> threading.Thread:
    """Start the daemon loop. Each iteration is try/except'd so one bad run
    never kills the thread; the once-per-day marker caps spend regardless."""

    def _loop():
        while True:
            try:
                result = run_once()
                if not result.get("skipped") or result.get("examined"):
                    print(f"[lead-enrich] {result}", flush=True)
                elif result.get("skipped") not in ("already ran today",):
                    print(f"[lead-enrich] {result.get('skipped')}", flush=True)
            except Exception as exc:
                print(f"[lead-enrich] loop error: {exc}", flush=True)
            time.sleep(interval_seconds)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()
    return thread
