"""Owner inbox: real mail from the connected Zoho mailbox, with AI reply drafts.

WHAT THIS IS
------------
The owner already reads mail in Zoho (MX points there), and `inbound_email`
polls that same mailbox so lead replies reach the pipeline. What was missing was
a place to answer one specific message: generate a reply that is about *that*
email, edit it, and send it back in the same thread.

This module is the read + draft + send half of that page (`/inbox`). It does
not poll, classify, or auto-send. Every send is an explicit owner POST.

THREADING
---------
Zoho's list endpoint does not return a thread id, and the Threads API needs a
folder id that the granted scopes cannot list. Grouping is therefore local:
counterpart address + subject with leading Re:/Fwd: removed. That is the same
thing a mail client does before it has thread ids, and it is stable across
reloads because it is derived, not remembered.

REPLIES
-------
When Zoho answers, the reply goes through POST /messages/{messageId} with
action=Reply, so it lands in the real conversation. If Zoho is not connected
the caller degrades to documents_service.send_email with a "Re:" subject --
the same transport the rest of the app uses.

NEVER INVENT
------------
The draft prompt forbids prices, dates, availability and commitments the thread
does not support. A holding reply is correct; a plausible guess is not.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from email.utils import parseaddr
from typing import Optional

import brand

# A single list call feeds every conversation in the left column. One minute is
# short enough that a mail arriving mid-session shows up on the next refresh and
# long enough that clicking through threads does not re-hit Zoho each time.
LIST_TTL_SECONDS = 60
_MAX_BODY = 12000
_MAX_THREAD_BODIES = 10

_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|sv|tr)\s*:\s*)+", re.I)
_WS = re.compile(r"\s+")
_NO_REPLY_RE = re.compile(
    r"^(no-?reply|do-?not-?reply|notifications?|mailer-daemon|postmaster|bounces?)@",
    re.I)
# Proton's daily "you have N new messages" notice is not mail from a person.
# It used to sit at the top of the inbox with a Reply badge and was answered by
# the lead bot. Hide this one sender; the owner can still see it in Zoho.
_PROTON_NOTIFICATION_RE = re.compile(r"^no-?reply@mail\.proton\.me$", re.I)

_list_cache: dict = {}
_list_lock = threading.Lock()


# ──────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────

def _mailbox() -> str:
    return (os.getenv("SMTP_FROM") or "").strip()


def _addr(raw: str) -> str:
    """Bare lower-case address from 'Name <addr>' or a bare address."""
    return (parseaddr((raw or "").strip())[1] or "").strip().lower()


def _display(raw: str) -> str:
    name, addr = parseaddr((raw or "").strip())
    return (name or "").strip() or addr


def subject_key(subject: str) -> str:
    """Subject with every Re:/Fwd: prefix removed, collapsed for grouping.

    Returned already lower-cased and whitespace-collapsed; it is a comparison
    key, not something to show. An empty result means "(no subject)" so two
    subject-less messages from one person still group together.
    """
    s = _SUBJECT_PREFIX.sub("", subject or "")
    s = _WS.sub(" ", s).strip().lower()
    return s or "(no subject)"


def reply_subject(subject: str) -> str:
    """'Re: ...' without stacking a second Re: on an already-replied subject."""
    s = (subject or "").strip()
    if not s:
        return f"Re: {brand.short_name()}"
    if re.match(r"^\s*re\s*:", s, re.I):
        return s
    return "Re: " + s


def conversation_id(party: str, key: str) -> str:
    h = hashlib.sha1(f"{(party or '').lower()}|{key or ''}".encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def _summary(text: str, limit: int = 150) -> str:
    s = _WS.sub(" ", (text or "").strip())
    return s[:limit] + ("…" if len(s) > limit else "")


# ──────────────────────────────────────────────
# Reading
# ──────────────────────────────────────────────

def _zoho():
    import zoho_mail
    return zoho_mail


def available() -> tuple:
    """(ok, reason). Kept separate so the page can say what to fix."""
    try:
        zm = _zoho()
    except Exception as exc:
        return False, f"Zoho Mail module unavailable: {exc}"
    try:
        if not zm.available():
            return False, "The Zoho connection lacks the Mail scope — reconnect it on Settings."
    except Exception as exc:
        return False, f"Could not read the Zoho connection: {exc}"
    return True, ""


def _heads(days: int, limit: int) -> tuple:
    """Cached recent-message list plus (account_id, address)."""
    days = max(1, min(int(days or 14), 365))
    limit = max(1, min(int(limit or 60), 200))
    cache_key = (days, limit)
    now = time.time()
    with _list_lock:
        hit = _list_cache.get(cache_key)
        if hit and now - hit["at"] < LIST_TTL_SECONDS:
            return hit["heads"], hit["account_id"], hit["address"]
    zm = _zoho()
    account_id, address = zm.account(_mailbox())
    heads = zm.list_recent(days=days, limit=limit)
    with _list_lock:
        _list_cache[cache_key] = {
            "at": now, "heads": heads, "account_id": account_id, "address": address,
        }
    return heads, account_id, address


def _cache_clear() -> None:
    with _list_lock:
        _list_cache.clear()


def conversations(days: int = 14, limit: int = 60) -> dict:
    """Recent mail grouped into conversations the owner can open and answer.

    Each row is one counterpart + subject, newest first, and says whether the
    ball is in the owner's court (last message inbound) or the customer's.
    """
    ok, reason = available()
    if not ok:
        return {"ok": False, "reason": reason, "items": [], "mailbox": _mailbox()}
    try:
        heads, _account_id, address = _heads(days, limit)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:300], "items": [],
                "mailbox": _mailbox()}

    me = (address or _mailbox()).strip().lower()
    groups: dict = {}
    for h in heads:
        mid = str(h.get("id") or "")
        if not mid:
            continue
        from_addr = _addr(h.get("from") or "")
        to_addr = _addr(h.get("to") or "")
        if _PROTON_NOTIFICATION_RE.match(from_addr):
            continue
        outbound = bool(me) and from_addr == me
        party = (to_addr if outbound else from_addr).strip()
        if not party or party == me:
            continue
        key = subject_key(h.get("subject") or "")
        gk = (party, key)
        g = groups.get(gk)
        if g is None:
            g = {
                "id": conversation_id(party, key),
                "party": party,
                "party_name": "",
                "key": key,
                "subject": (h.get("subject") or "").strip(),
                "messages": 0,
                "last_ms": 0.0,
                "last_direction": "in",
                "snippet": "",
            }
            groups[gk] = g
        g["messages"] += 1
        try:
            ms = float(h.get("received_ms") or 0)
        except (TypeError, ValueError):
            ms = 0.0
        if ms >= g["last_ms"]:
            g["last_ms"] = ms
            g["last_direction"] = "out" if outbound else "in"
            g["snippet"] = _summary(h.get("summary") or "")
            g["subject"] = (h.get("subject") or g["subject"]).strip()
            g["party_name"] = _display(h.get("to") if outbound else h.get("from"))

    items = sorted(groups.values(), key=lambda x: x["last_ms"], reverse=True)
    for g in items:
        g["when"] = time.strftime("%b %d, %H:%M", time.gmtime(g["last_ms"] / 1000)) if g["last_ms"] else ""
        g["needs_reply"] = (g["last_direction"] == "in"
                            and not _NO_REPLY_RE.match(g["party"]))
    return {"ok": True, "items": items, "mailbox": address or _mailbox(),
            "checked": len(heads)}


def _group_heads(party: str, key: str, days: int, limit: int) -> tuple:
    """Heads belonging to one conversation, oldest first."""
    heads, account_id, address = _heads(days, limit)
    me = (address or _mailbox()).strip().lower()
    party_l = (party or "").strip().lower()
    key_l = subject_key(key)
    out = []
    for h in heads:
        if not h.get("id"):
            continue
        from_addr = _addr(h.get("from") or "")
        to_addr = _addr(h.get("to") or "")
        outbound = bool(me) and from_addr == me
        other = (to_addr if outbound else from_addr).strip().lower()
        if other != party_l:
            continue
        if subject_key(h.get("subject") or "") != key_l:
            continue
        h = dict(h)
        h["direction"] = "out" if outbound else "in"
        h["other"] = other
        out.append(h)
    out.sort(key=lambda x: float(x.get("received_ms") or 0))
    return out, account_id, address


def thread(party: str, key: str, days: int = 90, limit: int = 200) -> dict:
    """One conversation with bodies, oldest first.

    Bodies are fetched live and capped: an old 40-message thread is context, not
    something the owner is about to scroll. Newest messages are the ones that
    matter, and the cap keeps the page to a handful of Zoho calls.
    """
    party = (party or "").strip().lower()
    if "@" not in party:
        return {"ok": False, "reason": "That conversation has no usable email address.", "messages": []}
    ok, reason = available()
    if not ok:
        return {"ok": False, "reason": reason, "messages": []}
    try:
        heads, _account_id, _address = _group_heads(party, key, days, limit)
    except Exception as exc:
        return {"ok": False, "reason": str(exc)[:300], "messages": []}

    if not heads:
        return {"ok": False, "reason": "That conversation is no longer in the mailbox window.",
                "messages": []}

    zm = _zoho()
    fetch_from = max(0, len(heads) - _MAX_THREAD_BODIES)
    messages = []
    for i, h in enumerate(heads):
        body = ""
        if i >= fetch_from:
            try:
                body = zm.message_text(h.get("account_id") or "", h.get("folder_id") or "",
                                       h.get("id") or "")
            except Exception as exc:
                body = (h.get("summary") or "") + f"\n\n[Body could not be read: {str(exc)[:120]}]"
        else:
            body = h.get("summary") or ""
        messages.append({
            "id": h.get("id") or "",
            "folder_id": h.get("folder_id") or "",
            "account_id": h.get("account_id") or "",
            "direction": h.get("direction") or "in",
            "from": h.get("from") or "",
            "from_name": _display(h.get("from")),
            "to": h.get("to") or "",
            "subject": h.get("subject") or "",
            "date": h.get("date") or "",
            "received_ms": h.get("received_ms") or 0,
            "body": (body or "")[:_MAX_BODY],
            "truncated": len(body or "") > _MAX_BODY,
        })
    latest_in = next((m for m in reversed(messages) if m["direction"] == "in"), None)
    subject = (messages[-1]["subject"] if messages else "") or key
    return {
        "ok": True,
        "party": party,
        "party_name": _display(messages[-1]["from"] if messages[-1]["direction"] == "in"
                               else messages[-1]["to"]) or party,
        "subject": subject,
        "reply_subject": reply_subject(subject),
        "messages": messages,
        "reply_to": latest_in["id"] if latest_in else "",
        "account_id": (latest_in or messages[-1]).get("account_id") or "",
        "folders_omitted": fetch_from,
    }


# ──────────────────────────────────────────────
# Drafting
# ──────────────────────────────────────────────

def _knowledge() -> str:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "construction_knowledge.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _transcript(messages: list, party: str) -> str:
    lines = []
    for m in messages[-12:]:
        who = "OWNER" if (m.get("direction") == "out") else (m.get("from_name") or party)
        when = m.get("date") or ""
        body = (m.get("body") or "").strip()
        lines.append(f"--- {when} | {who} ---\n{body}")
    return "\n\n".join(lines) or "(no earlier messages)"


def _system_prompt() -> str:
    manual = _knowledge()
    return f"""You draft an email reply for the owner of {brand.name()}, a licensed
general contractor in Hampton Roads, Virginia. The owner will read what you write
and send it to a real person, often without editing. Write the way a competent
contractor who respects the customer's time actually writes.

OUTPUT RULES — these are not style suggestions:
- Return ONLY the body of the email. No subject line, no markdown, no headings,
  no bullet-point decoration, no code fences, no "(draft)" label.
- Never use bracketed placeholders like [Name], [Date], [Amount], or [Your Name].
  If a fact is unknown, write around it instead of inventing a blank.
- Answer the most recent email specifically — address what that person actually
  asked, in the order they asked it. Do not send a generic intro.
- Keep it to roughly 3–8 sentences unless the thread genuinely needs more.
- Sign as the owner with the business phone: {os.getenv("COMPANY_PHONE") or "(757) 908-7121"}.
  Do not invent a signature block, title, or address.

TRUTH RULES — a wrong number is worse than a holding reply:
- Never invent prices, start dates, crew availability, permit outcomes, lead
  times, warranty terms, or commitments the thread does not support.
- If the email asks for something you cannot know from the thread or the manual,
  say plainly that you will check and follow up. That is a complete answer.
- Never claim work was done, scheduled, paid, or inspected unless the thread says so.
- Never mention being an AI, a model, or an automated assistant.

BUSINESS MANUAL (authoritative for services, policy and pricing):
{manual}
"""


def draft(party: str, key: str, instruction: str = "", tone: str = "professional",
          days: int = 90) -> dict:
    """Generate a reply body for one conversation. Never sends anything."""
    data = thread(party, key, days=days)
    if not data.get("ok"):
        return {"ok": False, "reason": data.get("reason") or "Conversation unavailable."}

    tone = (tone or "professional").strip().lower()
    if tone not in ("professional", "friendly", "brief", "firm"):
        tone = "professional"

    latest_in = next((m for m in reversed(data["messages"]) if m["direction"] == "in"), None)
    if latest_in is None:
        return {"ok": False,
                "reason": "There is no inbound message in this conversation to reply to."}

    ask = (instruction or "").strip()[:1500]
    user = (
        f"Conversation with {data.get('party_name') or party} <{party}>\n"
        f"Subject: {data.get('subject') or key}\n\n"
        f"THREAD (oldest first):\n{_transcript(data['messages'], party)}\n\n"
        f"The message you are replying to is the last OWNER-less (customer) entry above.\n"
        f"Requested tone: {tone}.\n"
    )
    if ask:
        user += f"The owner wants you to include this point: {ask}\n"
    else:
        user += ("The owner gave no extra instruction. Reply helpfully to what was actually "
                 "asked, and ask exactly one clarifying question if something is genuinely "
                 "missing.\n")

    try:
        from con_ai_agent import BusinessAIAgent
        agent = BusinessAIAgent(subset="copilot")
        response = agent._complete(
            [{"role": "system", "content": _system_prompt()},
             {"role": "user", "content": user}],
            max_tokens=700,
        )
        body = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        return {"ok": False, "reason": f"Draft failed: {str(exc)[:240]}"}

    body = _clean_draft(body)
    if not body:
        return {"ok": False, "reason": "The model returned an empty draft. Try again."}
    return {
        "ok": True,
        "subject": data.get("reply_subject") or reply_subject(data.get("subject") or key),
        "body": body,
        "party": party,
        "tone": tone,
    }


def _clean_draft(text: str) -> str:
    """Strip the wrappers models add even when told not to."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    # A model that opened with "Subject: ..." ignored the output rule; drop the
    # header rather than shipping it into the body where it reads like a stray.
    s = re.sub(r"^\s*subject\s*:.*\n+", "", s, flags=re.I)
    s = re.sub(r"^\s*(draft|email|reply)\s*:\s*", "", s, flags=re.I)
    return s.strip()


# ──────────────────────────────────────────────
# Sending
# ──────────────────────────────────────────────

def _lookup_lead_id(db, party: str) -> Optional[int]:
    if db is None:
        return None
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT id FROM leads WHERE LOWER(email) = %s AND company = 'construction' "
                "ORDER BY id DESC LIMIT 1;",
                ((party or "").strip().lower(),),
            )
            row = cur.fetchone()
        if not row:
            return None
        return row.get("id") if isinstance(row, dict) else row[0]
    except Exception:
        return None


def _recent_duplicate(db, party: str, body: str, seconds: int = 45) -> bool:
    """True when the exact same body just went to the same address.

    A double-click is the whole reason this exists; it is not a policy check.
    Only successful sends are logged, so this cannot suppress a retry after a
    failed transport.
    """
    if db is None:
        return False
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM comms_logs WHERE direction = 'outbound' AND channel = 'email' "
                "AND LOWER(recipient) = LOWER(%s) AND message_body = %s "
                "AND created_at > now() - make_interval(secs => %s) LIMIT 1;",
                (party, body, int(seconds)),
            )
            return cur.fetchone() is not None
    except Exception:
        # If the check cannot run, sending is still the owner's explicit choice.
        return False


def _log_sent(db, party: str, subject: str, body: str, lead_id: Optional[int]) -> None:
    if db is None:
        return
    try:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO comms_logs (direction, channel, sender, recipient, message_body, lead_id) "
                "VALUES ('outbound', 'email', %s, %s, %s, %s);",
                (_mailbox() or "owner", party, f"{subject}\n\n{body}"[:10000], lead_id),
            )
        db.commit()
    except Exception as exc:
        print(f"[inbox] sent to {party} but could not log it: {exc}", flush=True)


def _send_via_zoho(party: str, subject: str, body: str, key: str) -> dict:
    """Reply in-thread through Zoho. Returns {'ok', 'reason', 'fallback'}."""
    try:
        zm = _zoho()
        if not zm.available():
            return {"ok": False, "fallback": True,
                    "reason": "Zoho Mail is not connected."}
        heads, account_id, _address = _group_heads(party, key, 365, 200)
        target = next((h for h in reversed(heads) if h.get("direction") == "in"), None)
        if not target:
            return {"ok": False, "fallback": True,
                    "reason": "No inbound message in this conversation to reply to."}
        ok = zm.reply(account_id, target.get("id") or "", party, subject, body)
    except Exception as exc:
        return {"ok": False, "fallback": True, "reason": str(exc)[:300]}
    if not ok:
        return {"ok": False, "fallback": True,
                "reason": "Zoho did not accept the reply."}
    return {"ok": True, "transport": "Zoho Mail API", "in_reply_to": target.get("id") or ""}


async def send_reply(db, *, party: str, key: str, subject: str, body: str,
                     allow_fallback: bool = True) -> dict:
    """Send one owner-written reply. Called only from the explicit /inbox route.

    Zoho first (proper threading), then the app's normal transport with a Re:
    subject. Nothing is logged unless a transport actually accepted it -- the
    phantom "sent" rows in comms_logs were exactly what happened when that
    result was ignored.
    """
    party = (party or "").strip().lower()
    body = (body or "").strip()
    subject = (subject or "").strip()
    key = subject_key(key or subject)

    if "@" not in party or "." not in party.split("@", 1)[1]:
        return {"ok": False, "reason": "That conversation has no usable recipient address."}
    if not body:
        return {"ok": False, "reason": "The reply is empty."}
    if len(body) > 15000:
        return {"ok": False, "reason": "The reply is too long (15,000 character limit)."}
    if _recent_duplicate(db, party, body):
        return {"ok": False, "reason": "That exact reply was just sent to this address — "
                                       "check your inbox before sending it twice."}

    lead_id = _lookup_lead_id(db, party)
    result = await _to_thread(_send_via_zoho, party, subject, body, key)

    if not result.get("ok") and allow_fallback:
        result = await _send_via_app(party, subject, body)

    if not result.get("ok"):
        return {"ok": False, "reason": result.get("reason") or "Mail could not be sent."}

    _log_sent(db, party, subject, body, lead_id)
    _cache_clear()
    return {"ok": True, "to": party, "subject": subject,
            "transport": result.get("transport") or "email",
            "in_reply_to": result.get("in_reply_to") or "",
            "lead_id": lead_id}


async def _send_via_app(party: str, subject: str, body: str) -> dict:
    """The app's existing transport: Zoho send, Gmail API, then SMTP."""
    import documents_service
    cfg = documents_service.smtp_config_from_env()
    if not (cfg.get("SMTP_FROM") or "").strip():
        return {"ok": False, "reason": "No sending address is configured (SMTP_FROM is empty)."}
    try:
        delivered = await documents_service.send_email(cfg, party, subject, body)
    except Exception as exc:
        return {"ok": False, "reason": f"Send failed: {str(exc)[:240]}"}
    if delivered is not True:
        return {"ok": False,
                "reason": "The mail transport did not accept the message. Nothing was sent "
                          "or logged. Check the connected mailbox on Settings."}
    return {"ok": True, "transport": "app transport"}


async def _to_thread(fn, *args):
    import asyncio
    return await asyncio.to_thread(fn, *args)
