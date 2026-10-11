"""The one lead lifecycle, shared by every BizStack app.

Before this module each app had its own idea of what a lead *is*: the website
form wrote ``status='analyzed'``, the payment webhook wrote ``'deposit'``, the
permit importer wrote ``'new'`` into a *different* table, and the two apps used
overlapping-but-unequal status vocabularies. Nothing could answer "what do I do
with this lead next?" without the reader knowing which page it came from.

This module fixes that with two ideas:

``STAGES``
    A single ordered state machine. A lead is in exactly one stage. Everything
    else a business cares about (a deposit was paid, a project started, a permit
    was filed) is an *attribute or event*, never a stage. That is what stops the
    status column turning into a junk drawer.

``Next action``
    For every lead we can name the single thing the owner should do next, and
    when. The work queue is organised by next action, not by stage, because the
    owner's day is "contact these, follow up these, reply to these" -- not "show
    me every row whose status happens to be 'contacted'".

Nothing here touches a database. It is pure functions over a lead row plus its
comms, so both apps and the tests agree on the rules.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

# --- stages -----------------------------------------------------------------
# The canonical, ordered lifecycle. A lead is in exactly one of these.
NEW = "new"              # arrived; nothing has been sent yet
CONTACTED = "contacted"  # at least one outbound touch sent; no reply yet
ENGAGED = "engaged"      # the lead replied; the ball is in our court
QUOTED = "quoted"        # a price was sent; awaiting their decision
WON = "won"              # converted: host account created / project started
LOST = "lost"            # closed, or explicitly asked us to stop

STAGES = (NEW, CONTACTED, ENGAGED, QUOTED, WON, LOST)

STAGE_LABELS = {
    NEW: "New",
    CONTACTED: "Contacted",
    ENGAGED: "Replied",
    QUOTED: "Quoted",
    WON: "Won",
    LOST: "Lost",
}

# Stages that mean the lead has left the working pipeline.
CLOSED_STAGES = (WON, LOST)

# --- legacy translation -----------------------------------------------------
# Both apps have years of rows under their old vocabularies. Rather than a data
# migration that has to be right the first time, we translate on read: any
# status string maps into a stage. Anything unrecognised is treated as the start
# of the funnel, which is the safe default (it will be worked, not silently
# buried).
_LEGACY = {
    # hosts vocabulary
    "new": NEW,
    "analyzed": NEW,
    "contacted": CONTACTED,
    "quote": QUOTED,
    "quoted": QUOTED,
    "host": WON,
    "archive": LOST,
    "archived": LOST,
    # construction vocabulary
    "deposit": QUOTED,
    "in_progress": WON,
    "completed": WON,
    "lost": LOST,
    "replied": ENGAGED,
    "engaged": ENGAGED,
    "waiting": CONTACTED,
}


def to_stage(status) -> str:
    """Map any legacy/free-text status onto a canonical stage."""
    key = (status or "").strip().lower()
    if key in STAGES:
        return key
    return _LEGACY.get(key, NEW)


# --- next action ------------------------------------------------------------
# The owner-facing work buckets. Order matters: this is the order the queue
# lists them, most-urgent first.
ACTION_ENRICH = "enrich"            # no way to reach them yet
ACTION_FIRST_TOUCH = "first_touch"  # reachable, never contacted
ACTION_FOLLOW_UP = "follow_up"      # contacted, and a follow-up is now due
ACTION_RESPOND = "respond"          # they replied; answer them
ACTION_WAITING = "waiting"          # contacted/quoted, not due yet
ACTION_CLOSED = "closed"            # won or lost

ACTION_ORDER = (
    ACTION_RESPOND,
    ACTION_FOLLOW_UP,
    ACTION_FIRST_TOUCH,
    ACTION_ENRICH,
    ACTION_WAITING,
    ACTION_CLOSED,
)

ACTION_LABELS = {
    ACTION_RESPOND: "Replied — respond",
    ACTION_FOLLOW_UP: "Follow-up due",
    ACTION_FIRST_TOUCH: "Needs first contact",
    ACTION_ENRICH: "Needs contact info",
    ACTION_WAITING: "Waiting",
    ACTION_CLOSED: "Closed",
}

# How long after an outbound touch we wait before the follow-up becomes due.
# One number, in one place, rather than a magic literal in each app's sweep.
FOLLOW_UP_DAYS = 3


def is_reachable(lead) -> bool:
    """Whether anyone can actually reach this person.

    ``@lead.local`` is the shared placeholder convention for a lead that has no
    real contact details yet -- the permit importer writes
    ``con-permit-<hash>@lead.local`` and the reddit scan writes
    ``con-radar-...@lead.local``. It is not a mailbox, so a lead carrying only
    that email is not reachable and belongs in the ``enrich`` bucket, never the
    outreach sweep.
    """
    phone = (lead.get("phone") or "").strip()
    email = (lead.get("email") or "").strip().lower()
    if email.endswith("@lead.local"):
        email = ""
    return bool(phone or email)


def _as_dt(value):
    """Lead rows carry tz-aware timestamps, but a test or a legacy row may hand
    us a naive one; normalise both to tz-aware UTC so subtraction never raises."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def next_action(lead, *, now=None) -> tuple[str, datetime | None]:
    """Return ``(action, due_at)`` for a lead.

    ``due_at`` is only meaningful for the actions that are time-driven
    (first_touch, follow_up); the others return ``None``. The caller stores both
    so the queue can sort without re-deriving anything.
    """
    now = now or datetime.now(timezone.utc)
    stage = to_stage(lead.get("status"))

    if stage in CLOSED_STAGES:
        return ACTION_CLOSED, None
    if lead.get("do_not_contact"):
        return ACTION_CLOSED, None
    if stage == ENGAGED:
        # They replied. Nothing is time-driven here; answer whenever, but it
        # belongs at the top of the queue because a live human is waiting.
        return ACTION_RESPOND, None
    if not is_reachable(lead):
        return ACTION_ENRICH, None
    if stage == NEW:
        return ACTION_FIRST_TOUCH, _as_dt(lead.get("created_at")) or now

    # CONTACTED or QUOTED: due three days after we last reached out.
    last_out = _as_dt(lead.get("last_outbound_at")) or _as_dt(lead.get("created_at")) or now
    due = last_out + timedelta(days=FOLLOW_UP_DAYS)
    if now >= due:
        return ACTION_FOLLOW_UP, due
    return ACTION_WAITING, due


def allowed_stages(current, target) -> bool:
    """Whether a stage change is allowed.

    Deliberately permissive: the owner must always be able to close a lead and
    re-open a closed one, because real life does not follow the diagram. The one
    rule we enforce is that an unknown target never gets written.
    """
    return target in STAGES
