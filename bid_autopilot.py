"""Bid autopilot: fill in contact info and build documents for bids, unprompted.

The owner should never have to ask the Copilot for a name, email, phone, or a
SOW/itemized/contract set. This loop runs from lifecycle() every hour and, for
every active bid:

  1. CONTACT -- if contact_name, contact_email or contact_phone is missing and
     the bid has a parseable street address, runs the same path as the Copilot's
     research_bid_contact (skip trace -> Skip Sherpa -> save). Only owner-entered
     bids (source = 'manual'): scraped public RFPs have a city for a location
     and no homeowner, so tracing them just burns a credit.
  2. DOCUMENTS -- if the bid has cost lines and none of sow/itemized/contract
     yet, runs generate_bid_documents once. Never regenerates: an owner upload
     or a previous generation is left alone.

Each bid+kind is logged in bid_autopilot_log and not retried for 7 days, so a
bad address cannot cost a credit every hour. BID_AUTOPILOT_MAX caps traces per
pass (default 10). DISABLE_BID_AUTOPILOT=1 turns it off; parsed explicitly
because Railway stores an unset flag as the string "false".
"""
from __future__ import annotations

import logging
import os
import threading
import time

import psycopg
from psycopg.rows import dict_row

log = logging.getLogger("bid_autopilot")

ACTIVE_STATUSES = ("new", "drafting", "sent", "shortlist")
DOC_TYPES = ("sow", "itemized", "contract")
RETRY_DAYS = 7


def _env_flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in ("1", "true", "yes", "on")


def _max_traces() -> int:
    try:
        return max(0, int(os.getenv("BID_AUTOPILOT_MAX") or 10))
    except (TypeError, ValueError):
        return 10


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS bid_autopilot_log (
                id BIGSERIAL PRIMARY KEY,
                bid_id BIGINT NOT NULL,
                kind VARCHAR(20) NOT NULL,
                ok BOOLEAN NOT NULL DEFAULT FALSE,
                note TEXT,
                ran_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bid_autopilot_log_bid ON bid_autopilot_log(bid_id, kind, ran_at DESC);")
    conn.commit()


def _log(conn, bid_id: int, kind: str, ok: bool, note: str) -> None:
    with conn.cursor() as cur:
        cur.execute("INSERT INTO bid_autopilot_log (bid_id, kind, ok, note) VALUES (%s, %s, %s, %s)",
                    (bid_id, kind, ok, (note or "")[:500]))
    conn.commit()


def contact_candidates(conn, company: str, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT b.id, b.location, b.contact_name, b.contact_email, b.contact_phone
              FROM bid_opportunities b
             WHERE b.company = %s
               AND b.status = ANY(%s)
               AND COALESCE(b.source, 'manual') = 'manual'
               AND NULLIF(TRIM(b.location), '') IS NOT NULL
               AND (NULLIF(TRIM(COALESCE(b.contact_name, '')), '') IS NULL
                    OR NULLIF(TRIM(COALESCE(b.contact_email, '')), '') IS NULL
                    OR NULLIF(TRIM(COALESCE(b.contact_phone, '')), '') IS NULL)
               AND NOT EXISTS (
                    SELECT 1 FROM bid_autopilot_log l
                     WHERE l.bid_id = b.id AND l.kind = 'contact'
                       AND l.ran_at > now() - make_interval(days => {RETRY_DAYS}))
             ORDER BY b.created_at DESC
             LIMIT %s
        """, (company, list(ACTIVE_STATUSES), limit))
        return [dict(r) for r in cur.fetchall()]


def document_candidates(conn, company: str, limit: int) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT b.id
              FROM bid_opportunities b
             WHERE b.company = %s
               AND b.status = ANY(%s)
               AND EXISTS (SELECT 1 FROM bid_costs c WHERE c.bid_id = b.id AND c.company = b.company)
               AND NOT EXISTS (SELECT 1 FROM bid_documents d
                                WHERE d.bid_id = b.id AND d.document_type = ANY(%s))
               AND NOT EXISTS (
                    SELECT 1 FROM bid_autopilot_log l
                     WHERE l.bid_id = b.id AND l.kind = 'documents'
                       AND l.ran_at > now() - make_interval(days => {RETRY_DAYS}))
             ORDER BY b.created_at DESC
             LIMIT %s
        """, (company, list(ACTIVE_STATUSES), list(DOC_TYPES), limit))
        return [dict(r) for r in cur.fetchall()]


def run_once(db=None, limit=None) -> dict:
    """One pass. Never raises; returns a summary dict."""
    summary = {"contact_examined": 0, "contact_filled": 0, "docs_examined": 0,
               "docs_built": 0, "errors": [], "skipped": ""}
    if _env_flag("DISABLE_BID_AUTOPILOT"):
        summary["skipped"] = "DISABLE_BID_AUTOPILOT is set"
        return summary
    cap = _max_traces() if limit is None else max(1, min(int(limit), 25))
    if cap == 0:
        summary["skipped"] = "BID_AUTOPILOT_MAX is 0"
        return summary

    own = db is None
    if own:
        url = (os.getenv("DATABASE_URL") or "").strip()
        if not url:
            summary["skipped"] = "DATABASE_URL is not set"
            return summary
        db = psycopg.connect(url, row_factory=dict_row)

    try:
        # Imported here: construction_main imports this module at startup.
        from construction_main import _bid_document_tools, _BID_COMPANY
        import skiptrace_service as sts

        ensure_schema(db)
        owner = (os.getenv("OWNER_EMAIL") or "autopilot@buildstack").strip()
        tools = _bid_document_tools(db, owner)

        for bid in contact_candidates(db, _BID_COMPANY, cap):
            summary["contact_examined"] += 1
            parsed = sts.parse_address(bid["location"] or "")
            if not parsed.get("ok"):
                _log(db, bid["id"], "contact", False, f"unparseable address: {parsed.get('why')}")
                continue
            try:
                res = tools["research_bid_contact"](bid["id"])
            except Exception as e:  # noqa: BLE001
                db.rollback()
                res = {"ok": False, "error": repr(e)}
            if res.get("ok"):
                summary["contact_filled"] += 1
                _log(db, bid["id"], "contact", True,
                     f"name={res.get('owner_name')} email={res.get('email')} phone={res.get('phone')}")
            else:
                summary["errors"].append(f"#{bid['id']}: {res.get('error')}")
                _log(db, bid["id"], "contact", False, str(res.get("error")))

        for bid in document_candidates(db, _BID_COMPANY, 25):
            summary["docs_examined"] += 1
            try:
                res = tools["generate_bid_documents"](bid["id"])
            except Exception as e:  # noqa: BLE001
                db.rollback()
                res = {"ok": False, "error": repr(e)}
            if res.get("ok"):
                summary["docs_built"] += 1
                _log(db, bid["id"], "documents", True, ",".join(res.get("documents") or []))
            else:
                summary["errors"].append(f"#{bid['id']}: {res.get('error')}")
                _log(db, bid["id"], "documents", False, str(res.get("error")))
    except Exception as e:  # noqa: BLE001
        summary["errors"].append(repr(e))
        log.exception("bid_autopilot pass failed")
    finally:
        if own:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
    return summary


def start(interval_seconds: int = 3600) -> threading.Thread:
    def _loop():
        time.sleep(90)  # let startup DDL and the other threads settle first
        while True:
            try:
                s = run_once()
                if s.get("contact_examined") or s.get("docs_examined") or s.get("errors"):
                    log.info("bid_autopilot: %s", s)
            except Exception:  # noqa: BLE001
                log.exception("bid_autopilot loop error")
            time.sleep(interval_seconds)

    t = threading.Thread(target=_loop, daemon=True, name="bid-autopilot")
    t.start()
    return t
