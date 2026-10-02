"""Operator tool handlers for the Copilot that were previously unreachable.

Three problems this solves:

1. **Math.** An LLM doing arithmetic in its head is not good enough when the
   answer is a quote total, a payroll figure or a materials order. ``calculate``
   evaluates an expression in a restricted AST sandbox instead, so the numbers
   come out of Python, not out of the model.

2. **Modules that exist but were never wired.** ``google_maps.py`` (geocode,
   places, distance matrix, directions) and ``google_calendar.py`` (event CRUD,
   two-way sync) were fully written and reachable only from webhooks -- the
   Copilot had no tool schemas for them.

3. **The phone logs.** The 24/7 Vapi assistant's calls and texts already land in
   the ``comms_logs`` table. There is no need for Copilot to hold a conversation
   with the voice agent to read them; a direct query is faster, lossless and
   works even when Vapi is unreachable.
"""

import ast
import operator
import os
from datetime import datetime, timedelta, timezone

# google_calendar.py writes every event in Eastern time; match it.
TZ = "America/New_York"

# --- Restricted arithmetic --------------------------------------------------
# Only literal numbers, the operators below, and parentheses/functions.
# No names, no attribute access, no calls to anything outside _FUNCS, no
# comprehensions, no imports. Evaluating this is safe by construction.
_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}

_FUNCS = {
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "int": int,
    "float": float,
}
_CONSTANTS = {"pi": 3.141592653589793, "e": 2.718281828459045}

_MAX_POW = 10**6  # 10**1000000 is a cheap way to hang the worker


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise ValueError("only numbers are allowed")
        return node.value
    if isinstance(node, ast.BinOp):
        op = _BIN_OPS.get(type(node.op))
        if not op:
            raise ValueError(f"operator {type(node.op).__name__} not allowed")
        left, right = _eval(node.left), _eval(node.right)
        if op is operator.pow and (abs(right) > 100 or abs(left) > _MAX_POW):
            raise ValueError("exponent out of range")
        return op(left, right)
    if isinstance(node, ast.UnaryOp):
        op = _UNARY_OPS.get(type(node.op))
        if not op:
            raise ValueError(f"operator {type(node.op).__name__} not allowed")
        return op(_eval(node.operand))
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCS:
            raise ValueError("only abs/round/min/max/sum/int/float are callable")
        if node.keywords:
            raise ValueError("keyword arguments are not allowed")
        return _FUNCS[node.func.id](*[_eval(a) for a in node.args])
    if isinstance(node, ast.Name):
        if node.id in _CONSTANTS:
            return _CONSTANTS[node.id]
        raise ValueError(f"unknown name {node.id!r}")
    if isinstance(node, (ast.List, ast.Tuple)):
        return [_eval(e) for e in node.elts]
    raise ValueError(f"{type(node).__name__} is not allowed")


def calculate(expression: str = "") -> dict:
    """Evaluate a math expression exactly. Use this for every computed figure."""
    if not (expression or "").strip():
        return {"ok": False, "error": "No expression given."}
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        value = _eval(tree)
    except ZeroDivisionError:
        return {"ok": False, "error": "Division by zero."}
    except (SyntaxError, ValueError, TypeError, OverflowError, RecursionError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # never let a math question take the worker down
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"ok": True, "expression": expression.strip(), "result": value}


# Search models, cheapest first. `OPENAI_SEARCH_MODEL` overrides the whole list.
#
# `gpt-4o-mini-search-preview` and `web_search_preview` are what this used to
# call, and both were shut down on 2026-07-23 -- which is why the Copilot spent
# a stretch telling the owner it "cannot perform a web search." Every model here
# is a normal model that supports the hosted `web_search` tool.
_SEARCH_MODELS = ("gpt-4.1-mini", "gpt-4.1", "gpt-5.5")

# Personal-profile and people-search domains, blocked at the search layer.
#
# The owner decided lead sourcing is business contacts only: a roofer or plumber
# with a real business line is the lead, not a homeowner. Putting this in the
# tool's `filters` rather than in the prompt means the Copilot cannot be talked
# into it by a conversation -- the results are simply never returned. Prompt
# instructions are a preference; a blocked domain is a boundary.
_BLOCKED_CONTACT_DOMAINS = (
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "pinterest.com", "tiktok.com", "reddit.com",
    "whitepages.com", "truepeoplesearch.com", "fastpeoplesearch.com",
    "spokeo.com", "mylife.com", "beenverified.com", "nuwber.com",
    "clustrmaps.com", "radaris.com", "locatefamily.com", "lusha.com",
    "contactout.com", "clearbit.com", "apollo.io", "zoominfo.com",
    "peoplefinder.com", "usphonebook.com", "thatpeoplesearch.com",
)


def _search_models() -> tuple:
    override = (os.getenv("OPENAI_SEARCH_MODEL") or "").strip()
    return (override,) if override else _SEARCH_MODELS


def _search_tool() -> dict:
    """The hosted web_search tool config, including the contact-source blocks."""
    raw = os.getenv("SEARCH_BLOCKED_DOMAINS")
    domains = ([d.strip().lower() for d in raw.split(",") if d.strip()]
               if raw is not None else list(_BLOCKED_CONTACT_DOMAINS))
    return {
        "type": "web_search",
        "search_context_size": "low",
        "filters": {"blocked_domains": domains},
    }


def _extract_citations(response) -> list:
    """Pull the cited URLs out of a Responses object.

    `output` is a list of items; a message item's `content` is itself a *list*
    of parts, and the annotations hang off each part. The previous version
    looked for a `.part` attribute on `content`, which never exists, so this
    silently returned an empty list on every successful search -- the tool was
    working and reporting no sources.
    """
    citations = []
    for item in getattr(response, "output", None) or []:
        parts = getattr(item, "content", None) or []
        if not isinstance(parts, (list, tuple)):
            continue
        for part in parts:
            for ann in getattr(part, "annotations", None) or []:
                url = getattr(ann, "url", None)
                if url and url not in citations:
                    citations.append(url)
    return citations


def build_web_search_tools():
    """OpenAI-hosted web search, as a normal tool the existing tool loop can call.

    Deliberately isolated. The Copilot's main loop is Chat Completions; this is a
    single self-contained Responses-API call whose result comes back as an
    ordinary tool message. If every model is rejected or the call fails for any
    reason, the tool returns a clean error the model can report instead of taking
    the assistant down.

    Tries the model list in order and falls through on a *rejected model*, not on
    a general failure: a bad query or a rate limit should surface, not silently
    burn three more billed calls.
    """

    def web_search(query: str = "") -> dict:
        """Search the live web. Use for current prices, codes, rules, suppliers,
        anything that changed after training. Cite what you find.

        Business contacts only. For finding a company to work with, look for the
        company's own site and its public business listing -- not personal
        profiles or people-search sites.
        """
        if not (query or "").strip():
            return {"ok": False, "error": "No search query given."}
        try:
            from openai import OpenAI

            client = OpenAI()
        except Exception as e:
            return {"ok": False,
                    "error": f"Web search unavailable (client init failed: "
                              f"{type(e).__name__}: {e})."}

        last = ""
        for model in _search_models():
            try:
                response = client.responses.create(
                    model=model,
                    # `tool_choice: required` because the whole point of this
                    # call is the search. Left on auto, the model is free to
                    # answer from memory and return no sources at all.
                    tools=[_search_tool()],
                    tool_choice="required",
                    input=query.strip(),
                )
                break
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                # Model not found / no access: worth trying the next one.
                if "model" not in last.lower() and "does not exist" not in last.lower():
                    return {"ok": False,
                            "error": f"Web search unavailable ({last}). "
                                      f"Tell the owner plainly rather than guessing."}
        else:
            return {"ok": False, "error": f"Web search unavailable ({last})."}

        answer = (getattr(response, "output_text", "") or "").strip()
        if not answer:
            return {"ok": False, "error": "Web search returned nothing."}
        return {"ok": True, "query": query.strip(), "answer": answer,
                "citations": _extract_citations(response)}

    return {"web_search": web_search}


# --- Comms log (calls + texts, read straight from Postgres) ------------------
def build_comms_log_tools(db):
    """Read the phone lines. Calls and texts share the comms_logs table."""

    def search_comms(query: str = "", channel: str = "", days: int = 30, limit: int = 25) -> dict:
        """Search recent calls and texts. `channel` is 'voice', 'sms' or 'email'; blank = all."""
        limit = max(1, min(int(limit or 25), 100))
        days = max(1, min(int(days or 30), 730))
        sql = (
            "SELECT id, direction, channel, sender, recipient, message_body, created_at "
            "FROM comms_logs WHERE created_at > NOW() - (%s || ' days')::interval"
        )
        params: list = [str(days)]
        channel = (channel or "").strip().lower()
        if channel in ("voice", "sms", "email"):
            sql += " AND channel = %s"
            params.append(channel)
        query = (query or "").strip()
        if query:
            sql += (
                " AND (sender ILIKE %s OR recipient ILIKE %s"
                " OR COALESCE(message_body,'') ILIKE %s)"
            )
            like = f"%{query}%"
            params += [like, like, like]
        sql += " ORDER BY created_at DESC LIMIT %s;"
        params.append(limit)

        with db.cursor() as cur:
            cur.execute(sql, tuple(params))
            rows = cur.fetchall()

        out = []
        for r in rows:
            body = (r.get("message_body") or "").strip()
            out.append({
                "id": r["id"],
                "direction": r["direction"],
                "channel": r["channel"],
                "other_party": r["recipient"] if r["direction"] == "outbound" else r["sender"],
                "when": r["created_at"].isoformat() if r.get("created_at") else "",
                "transcript": body[:2000],
            })
        return {"ok": True, "count": len(out), "results": out}

    return {"search_comms": search_comms}


# --- Google Maps ------------------------------------------------------------
def build_maps_tools(db):
    """Wire the existing google_maps module into the Copilot.

    Calendar service needs the owner's email for token lookup; Maps needs no
    per-user credentials.
    """

    def maps_geocode(address: str = "") -> dict:
        """Get latitude/longitude and a formatted address for a street address."""
        import google_maps

        if not address.strip():
            return {"ok": False, "error": "What is the address?"}
        result = google_maps.geocode_address(address.strip())
        if not result:
            return {"ok": False, "error": f"Couldn't find that address: {address}"}
        return {"ok": True, **result}

    def maps_find_place(name: str = "", address: str = "") -> dict:
        """Find a nearby business by name (supplier, store, inspector) with phone and website."""
        import google_maps

        if not (name or "").strip():
            return {"ok": False, "error": "What should I look for?"}
        place = google_maps.find_place_by_name_and_address(name.strip(), (address or "").strip())
        if not place:
            return {"ok": False, "error": f"No match for {name}."}
        return {"ok": True, **place}

    def maps_directions(origin: str = "", destination: str = "") -> dict:
        """Driving distance and minutes between two addresses, plus a clickable directions URL."""
        import google_maps

        if not origin.strip() or not destination.strip():
            return {"ok": False, "error": "Need both an origin and a destination address."}
        origin, destination = origin.strip(), destination.strip()
        a, b = google_maps.geocode_address(origin), google_maps.geocode_address(destination)
        if not a or not b:
            missing = origin if not a else destination
            return {"ok": False, "error": f"Couldn't geocode {missing}."}
        try:
            # distance_matrix takes lists of "lat,lng" or addresses and raises on
            # failure rather than reporting "no route" -- surface that honestly.
            matrix = google_maps.distance_matrix([origin], [destination]) or {}
        except Exception as e:
            return {"ok": False, "error": f"Directions unavailable: {e}"}
        return {
            "ok": True,
            "origin": a.get("formatted_address") or origin,
            "destination": b.get("formatted_address") or destination,
            "directions_url": google_maps.maps_directions_url(
                a["lat"], a["lng"], b["lat"], b["lng"]
            ),
            **matrix,
        }

    return {
        "maps_geocode": maps_geocode,
        "maps_find_place": maps_find_place,
        "maps_directions": maps_directions,
    }


# --- Google Calendar --------------------------------------------------------
def build_calendar_tools(db):
    """Wire google_calendar into the Copilot. Requires the owner's Google OAuth grant."""
    DEFAULT_CALENDAR = "primary"

    def _impl():
        import google_calendar

        return google_calendar

    def list_calendar_events(start: str = "", end: str = "", days: int = 7) -> dict:
        """Read the owner's Google Calendar for the next `days` (default 7)."""
        gc = _impl()
        try:
            now = datetime.now(timezone.utc)
            window_start = _parse_dt(start, None) or now
            window_end = _parse_dt(end, None) or (
                now + timedelta(days=max(1, min(int(days or 7), 90)))
            )
            # list_events takes ISO 8601 strings, matching google_calendar.py's
            # own sync path which passes .isoformat().
            events = gc.list_events(
                "google", "owner", DEFAULT_CALENDAR,
                time_min=window_start.isoformat(),
                time_max=window_end.isoformat(),
            )
        except Exception as e:
            return {"ok": False, "error": f"Calendar unavailable: {e}"}
        return {"ok": True, "count": len(events or []), "events": events or []}

    def schedule_event(
        summary: str = "", start: str = "", end: str = "", description: str = "",
        attendees: str = "", location: str = "",
    ) -> dict:
        """Create a calendar event (walkthrough, crew shift, host turnover, training)."""
        gc = _impl()
        if not summary.strip():
            return {"ok": False, "error": "The event needs a title."}
        start_dt = _parse_dt(start, None)
        if start_dt is None:
            return {"ok": False, "error": f"The event needs a start time I can read: {start or '(missing)'}"}
        end_dt = _parse_dt(end, None)
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)

        # Raw Google Calendar event shape, matching _bizstack_event_to_google().
        event = {
            "summary": summary.strip(),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": TZ},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": TZ},
        }
        if description.strip():
            event["description"] = description.strip()
        if location.strip():
            event["location"] = location.strip()
        invitees = [a.strip() for a in attendees.split(",") if a.strip()]
        if invitees:
            event["attendees"] = [
                {"email": a} if "@" in a else {"displayName": a} for a in invitees
            ]

        try:
            created = gc.create_event("google", "owner", DEFAULT_CALENDAR, event)
        except Exception as e:
            return {"ok": False, "error": f"Could not create the event: {e}"}
        return {
            "ok": True,
            "message": f"Scheduled '{summary.strip()}' on the calendar.",
            "event_id": (created or {}).get("id"),
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "attendees": event.get("attendees", []),
        }

    def cancel_event(event_id: str = "") -> dict:
        """Delete a calendar event by id."""
        gc = _impl()
        if not event_id.strip():
            return {"ok": False, "error": "Need the event id."}
        try:
            gc.delete_event("google", "owner", DEFAULT_CALENDAR, event_id.strip())
        except Exception as e:
            return {"ok": False, "error": f"Could not delete that event: {e}"}
        return {"ok": True, "message": f"Removed event {event_id.strip()}."}

    def sync_calendars() -> dict:
        """Two-way sync of all host property bookings to Google Calendar."""
        gc = _impl()
        try:
            return {"ok": True, **(gc.sync_all_properties("google", "owner", db) or {})}
        except Exception as e:
            return {"ok": False, "error": f"Calendar sync failed: {e}"}

    return {
        "list_calendar_events": list_calendar_events,
        "schedule_event": schedule_event,
        "cancel_event": cancel_event,
        "sync_calendars": sync_calendars,
    }


def _parse_dt(raw: str, default):
    """Accept ISO 8601 (with Z) or a bare local 'YYYY-MM-DD HH:MM'."""
    if raw is None:
        return default
    text = str(raw).strip()
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return default
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- Task memory ------------------------------------------------------------
def build_task_tools(db, owner_email: str):
    """The one-year task list. Owner-scoped; the email is bound here, not by the model."""
    import copilot_tasks

    def add_task(title: str = "", detail: str = "", category: str = "general",
                 due_at: str = "", entity_type: str = "", entity_id: str = "") -> dict:
        """Remember something the owner asked to be done or chased. Survives for a year."""
        if not title.strip():
            return {"ok": False, "error": "The task needs a title."}
        task_id = copilot_tasks.add_task(
            db, owner_email, title.strip(), detail.strip(),
            (category or "general").strip().lower()[:40] or "general",
            _parse_dt(due_at, None), entity_type.strip()[:40], str(entity_id or "")[:40],
        )
        return {"ok": True, "task_id": task_id, "title": title.strip()}

    def list_tasks(status: str = "open", category: str = "") -> dict:
        """List the owner's remembered tasks. `status` is open, done, dropped or all."""
        import copilot_tasks as ct

        rows = ct.list_tasks(db, owner_email, (status or "open").strip().lower(), category.strip())
        return {"ok": True, "count": len(rows), "tasks": rows}

    def complete_task(task_id: str = "", result: str = "", status: str = "done") -> dict:
        """Close out a task by its id."""
        import copilot_tasks as ct

        try:
            found = ct.update_task(db, owner_email, int(task_id), status.strip().lower(), result)
        except (TypeError, ValueError) as e:
            return {"ok": False, "error": str(e)}
        if not found:
            return {"ok": False, "error": f"No task #{task_id} on your list."}
        return {"ok": True, "message": f"Task #{task_id} marked {status}."}

    return {"add_task": add_task, "list_tasks": list_tasks, "complete_task": complete_task}