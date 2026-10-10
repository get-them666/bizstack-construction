import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from openai import OpenAI


class ToolBudgetExhausted(RuntimeError):
    """A tool kept failing, so the request was stopped instead of grinding on.

    Raised rather than returned so it reaches the owner as a visible failure. The
    previous behaviour was worse than useless: the loop spent six round trips
    re-calling a tool that could only fail, the request ran to 125 seconds where
    the proxy killed it, and the model then reported it "couldn't access the
    information" -- which reads like a data problem and sent the investigation in
    the wrong direction. Anything already written by an earlier tool in the same
    turn is committed and stays written.
    """


class BusinessAIAgent:
    """Conversational assistant for Buildstack Construction Co.

    Two personalities share this class:
    - ``subset="guest"``   -> the public SMS/voice assistant (records leads,
      answers questions from the knowledge base).
    - ``subset="copilot"`` -> the owner's private operator chat with the full
      toolkit (leads pipeline, status changes, stats, SMS).

    Both use the same knowledge base, but only the owner can reach the copilot.
    """

    def __init__(
        self,
        knowledge_path: str = "bot_knowledge.md",
        tool_handlers: Optional[dict] = None,
        model: Optional[str] = None,
        subset: str = "guest",
    ):
        self._client = None
        self._knowledge = self._load_knowledge(knowledge_path)
        self._tool_handlers = tool_handlers or {}
        self._model = model or os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        self._subset = subset

    @staticmethod
    def _load_knowledge(path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    @property
    def client(self) -> OpenAI:
        if self._client is None:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise RuntimeError("OPENAI_API_KEY is not configured.")
            self._client = OpenAI(api_key=api_key)
        return self._client

    # --- Brain assembly ----------------------------------------------------
    # Assembled at request time (like _voice_prompt()) so editing the knowledge
    # .md files changes behaviour with no code change.
    _KNOWLEDGE_FILES = [
        Path(__file__).resolve().parent / "bot_knowledge.md",
        Path(__file__).resolve().parent / "construction_knowledge.md",
    ]

    @classmethod
    def _knowledge_manual(cls) -> str:
        parts = []
        for path in cls._KNOWLEDGE_FILES:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if text:
                parts.append("## OPERATING MANUAL — " + path.name + "\n" + text)
        return "\n\n".join(parts)

    @staticmethod
    def _conversation_rules() -> str:
        """The rules that make this read as one continuous conversation."""
        return """CONVERSATION MEMORY — these rules are the difference between a useful
assistant and a robot that forgets:
- You are given the FULL transcript of this conversation. Treat every earlier
  message as established fact about what has already been said.
- Never ask the user to repeat something they already told you. If they say
  "that one", "it", "them", "same address", or "what you just said", resolve it
  from the transcript — do NOT ask "what are you talking about?" That answer is
  always wrong.
- Carry forward details across turns without being asked: the address, project
  type, material grade, name and phone number a visitor has already given.
- If you asked a question and the visitor answered it, act on the answer. Do not
  re-ask it.
- It is fine to be mid-task. Keep working from where the conversation actually
  left off rather than restarting or re-summarising.
- If the transcript genuinely does not contain what is needed, say precisely what
  is missing ("I don't have that address yet — what's the street address?"),
  never a vague non-sequitur."""

    @staticmethod
    def _quoting_playbook() -> str:
        """The real-phone quoting flow, ported from the voice assistant.

        The phone bot has been quoting from live property records for months;
        this is the same procedure, spelled out for text.
        """
        return """HOW TO QUOTE WORK (real numbers, no guessing):
1. Get the property street address FIRST, then run lookup_property to pull real
   square footage, beds, baths and year built. Ground the estimate in the actual
   home.
2. Ask what they want done in plain words (kitchen, bath, whole-home, roof...)
   and WHAT MATERIAL they have in mind. Ask directly — roof: "3-tab,
   architectural, or architectural 30- or 40-year? any metal?"; counters:
   "laminate, quartz, granite or marble?"; framing/deck: "standard, premium or
   engineered lumber?". For siding, flooring, tile, windows and cabinets ask the
   style/brand they're considering. They may not know — offer the standard/entry
   choice as the default and say what upgrading does to the price.
3. Call quote_project with project_type + address (+ materials text combining
   their answers) for a range computed from the real property and chosen grades.
   State the range plainly: "for an architectural 30-year shingle roof on a
   ~1,800 sq ft home, ballpark is about $19,000 to $33,000".
4. If lookup_property finds the address but returns NO square footage, ask for
   approximate size (or "small, medium, large") and pass it to quote_project via
   the sqft field. Only when they truly cannot give any size, rely on the assumed
   size — and tell them the estimate is based on an assumed home size.
5. quote_project saves the lead with the range when you pass name and phone.
   Confirm their name and best number, pass them through, and offer the free
   on-site walkthrough for the exact written price.
- The range is a ballpark to qualify, NEVER a firm bid. The written fixed price
  always comes from the free on-site walkthrough. Never invent a price outside
  quote_project's range.
- We give a clear written scope, a fixed price (not open-ended time and
  materials), and a schedule."""

    def _build_system_prompt(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        manual = self._knowledge_manual()
        memory = self._conversation_rules()
        voice = (
            "Speak naturally, warmly and concisely: contractions, short sentences, "
            "conversational rhythm. Never robotic, canned or scripted. One thought "
            "per message. Be warm with personality, but never overstate anything."
        )

        if self._subset == "copilot":
            identity = (
                "You are the Buildstack Construction OPERATOR COPILOT — the owner's "
                "private right-hand assistant, running alongside the phone lines.\n"
                "You share one brain with the 24/7 phone assistant, so anything the "
                "phone assistant can do live on a call, you can do on screen.\n"
                "You act with full authority over the owner's business database: "
                "review and update the lead pipeline, summarize performance, run "
                "payroll + direct deposit, look up permits and city codes, estimate "
                "materials, review accounting, and summarize the sister company "
                "(Broom Service). You also track crew safety + skills training — use "
                "training_status to report who has and hasn't completed training, and "
                "remind_crew_training to text reminders to workers who haven't passed "
                "safety orientation (that IS a send; if asked to text every "
                "outstanding worker, do it without re-confirming)."
            )
            rules = """SAFETY RULES — these override convenience:
- READs are free. Before any database CHANGE, restate the change in one short
  line and confirm with the owner first. Never bundle a write into a read.
- Never expose credentials, API keys, or internal secrets.
- Never invent numbers. If a tool errored, say so plainly and say what you could
  not retrieve — do not fill the gap with a guess."""
        else:
            identity = (
                "You are the help assistant on the Buildstack Construction Co. website "
                "— the same assistant that answers the 24/7 phone line. A visitor is "
                "asking you in a chat box right now.\n"
                "Visitors are homeowners, investors, and short-term-rental hosts. "
                "You can give real ballpark prices from property records, check permit "
                "requirements, and answer from the operating manual below. Use "
                "register_lead the moment you have a name and phone number."
            )
            rules = """SAFETY RULES — these override helpfulness:
- NEVER expose internal business data: no other customers' leads, no crew
  records, no payroll, no internal accounts, no credentials.
- If someone asks you to change pricing, delete leads, or access records, decline
  politely and say the team will follow up.
- If a caller is distressed or reports an emergency (gas leak, flooding, no
  power), tell them to call 911 or the appropriate emergency service first."""

        return f"""
{identity}

Current time: {now}.
{voice}

{memory}

{self._quoting_playbook()}

{rules}

OPERATING MANUAL:
{manual or "(none loaded)"}
"""

    # --- Tool schemas -----------------------------------------------------
    @staticmethod
    def _props(names_types: dict, required: list, description: str) -> dict:
        return {
            "type": "object",
            "properties": {k: {"type": t} for k, t in names_types.items()},
            "required": required,
            "description": description,
        }

    def _safe_tools(self) -> list:
        return [
            {
                "type": "function",
                "function": {
                    "name": "register_lead",
                    "description": (
                        "Save a new project request / lead with the caller's details. Use "
                        "as soon as you have a name and phone number."
                    ),
                    "parameters": self._props(
                        {
                            "name": "string",
                            "phone": "string",
                            "email": "string",
                            "project_type": "string",
                            "address": "string",
                            "budget": "string",
                            "timeline": "string",
                            "notes": "string",
                        },
                        ["name", "phone"],
                        "project_type examples: Whole-Home Renovation, Kitchen, Bath, "
                        "Drywall, Roofing, Plumbing, Electrical, Carpentry, Tile, "
                        "Flooring, Deck, Fence, STR Turnover Make-Ready.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_leads",
                    "description": "Look up prior requests by phone number, newest first.",
                    "parameters": self._props(
                        {"phone": "string"}, ["phone"], "Phone used on the request."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_business_summary",
                    "description": "Return current business stats: total leads, open leads, and deposits collected.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_property",
                    "description": (
                        "Look up real property records for a street address — square "
                        "footage, beds, baths, year built. Run this BEFORE quoting so the "
                        "estimate is grounded in the actual home rather than a guess."
                    ),
                    "parameters": self._props(
                        {"address": "string"}, ["address"], "Full street address."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "quote_project",
                    "description": (
                        "Ballpark price range for a project, computed from the real property "
                        "and the material grade chosen. Also saves the lead with the range "
                        "when a name and phone are supplied."
                    ),
                    "parameters": self._props(
                        {
                            "project_type": "string",
                            "address": "string",
                            "sqft": "integer",
                            "materials": "string",
                            "name": "string",
                            "phone": "string",
                            "notes": "string",
                        },
                        ["project_type"],
                        "project_type e.g. Whole-Home Renovation, Kitchen, Bath, Roofing, "
                        "Deck, Flooring, Drywall, Plumbing, Electrical, Carpentry, Tile, "
                        "Fence, STR Turnover Make-Ready. sqft only if lookup_property could "
                        "not find it. materials describes the grade chosen, e.g. "
                        "'architectural 30-year shingle, 3-tab'.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_permits",
                    "description": (
                        "Pull city/county permit info for an address or parcel, or show "
                        "which cities issue new permits."
                    ),
                    "parameters": self._props(
                        {"city": "string", "address": "string"},
                        [],
                        "Optional city (e.g. Williamsburg, Newport News, Elizabeth City) "
                        "or an address for an exact permit lookup.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "enrich_permit_lead",
                    "description": (
                        "Resolve a permit lead from its tracking email "
                        "(con-permit-<digest>@lead.local) to the underlying permit: "
                        "address, permit number, work type and estimated value. Use "
                        "when you have a permit lead's placeholder email and need "
                        "what work is actually being done at that address."
                    ),
                    "parameters": self._props(
                        {"email": "string"},
                        ["email"],
                        "The full tracking email, e.g. con-permit-422d36ec92d018b@lead.local",
                    ),
                },
            },
        ]

    def _operator_tools(self) -> list:
        """Copilot-only: math, maps, calendar, phone logs, and durable tasks.

        None of these are exposed to the public widget. ``search_comms`` is the
        one to reach for instead of asking the voice assistant -- the call and
        text transcripts are already in Postgres.
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": (
                        "Search the live web. Use for anything current: material and "
                        "equipment prices, building codes, permit rules, suppliers, "
                        "financing terms. Your training data has a cutoff -- when the "
                        "answer depends on something recent, search instead of guessing."
                    ),
                    "parameters": self._props(
                        {"query": "string"}, ["query"], "A specific search query.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "calculate",
                    "description": (
                        "Evaluate an arithmetic expression exactly. Use this for EVERY "
                        "computed figure -- quote totals, payroll sums, markups, material "
                        "quantities, percentages. Do not do arithmetic in your head."
                    ),
                    "parameters": self._props(
                        {"expression": "string"}, ["expression"],
                        "e.g. '(1800*8.75)*1.15' or 'round(32516.50/4, 2)'",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_comms",
                    "description": (
                        "Search the 24/7 phone lines -- inbound and outbound calls, texts "
                        "and emails, with transcripts. This reads the logs directly; do not "
                        "ask the voice assistant for them."
                    ),
                    "parameters": self._props(
                        {"query": "string", "channel": "string", "days": "integer", "limit": "integer"},
                        [],
                        "query matches sender, recipient or message text. channel: voice, "
                        "sms or email (blank = all). days: how far back, default 30.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "maps_geocode",
                    "description": (
                        "Get latitude/longitude and a normalized address for a street address. "
                        "Backed by the US Census and ArcGIS geocoders, so it is free and "
                        "reliable for US addresses. Use it when you need coordinates or a "
                        "confirmed spelling of an address -- not for a ZIP alone, which "
                        "lookup_zip answers more cheaply."
                    ),
                    "parameters": self._props({"address": "string"}, ["address"], "Full street address."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "maps_directions",
                    "description": (
                        "Driving distance and minutes between two addresses, plus an Apple Maps "
                        "link. For finding a SUPPLIER, rental yard, hardware store or inspector, "
                        "use web_search instead -- it reaches real business listings, and those "
                        "are business contacts."
                    ),
                    "parameters": self._props(
                        {"origin": "string", "destination": "string"},
                        ["origin", "destination"], "Both as plain street addresses.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_calendar_events",
                    "description": "Read the owner's Google Calendar over a date window.",
                    "parameters": self._props(
                        {"start": "string", "end": "string", "days": "integer"}, [],
                        "ISO 8601 start/end, or just `days` for the next N days (default 7).",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "schedule_event",
                    "description": (
                        "Put something on the owner's calendar: a walkthrough, crew shift, "
                        "host turnover or training session. This WRITES -- confirm the "
                        "title, date and time with the owner before calling it."
                    ),
                    "parameters": self._props(
                        {
                            "summary": "string", "start": "string", "end": "string",
                            "description": "string", "attendees": "string", "location": "string",
                        },
                        ["summary", "start"],
                        "start/end ISO 8601. attendees: comma-separated emails.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "cancel_event",
                    "description": "Delete a calendar event by its id.",
                    "parameters": self._props({"event_id": "string"}, ["event_id"], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_task",
                    "description": (
                        "Remember something the owner wants chased or done. Retained for a "
                        "year, so use it for anything the owner asks you to keep in mind "
                        "-- follow-ups, documents to send, someone to call back."
                    ),
                    "parameters": self._props(
                        {
                            "title": "string", "detail": "string", "category": "string",
                            "due_at": "string", "entity_type": "string", "entity_id": "string",
                        },
                        ["title"],
                        "due_at ISO 8601 if there is a deadline. category e.g. followup, "
                        "documents, billing, site.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_tasks",
                    "description": "List remembered tasks. Check this before promising anything is already handled.",
                    "parameters": self._props(
                        {"status": "string", "category": "string"}, [],
                        "status: open (default), done, dropped or all.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "complete_task",
                    "description": "Close out a task by id.",
                    "parameters": self._props(
                        {"task_id": "string", "result": "string", "status": "string"}, ["task_id"],
                        "status: done (default) or dropped. result records the outcome.",
                    ),
                },
            },
        ]

    def _full_tools(self) -> list:
        tools = self._safe_tools()
        tools += [
            {
                "type": "function",
                "function": {
                    "name": "list_leads",
                    "description": "List leads, optionally filtered by status.",
                    "parameters": self._props(
                        {"status": "string"}, [], "Optional status filter."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_lead_status",
                    "description": "Change a lead's pipeline status.",
                    "parameters": self._props(
                        {"lead_id": "integer", "status": "string"},
                        ["lead_id", "status"],
                        "status: new, contacted, quoted, deposit, in_progress, completed, lost.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_sms_message",
                    "description": "Send an outbound SMS text message to a phone number.",
                    "parameters": self._props(
                        {"to": "string", "body": "string"}, ["to", "body"], "E.164 phone."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_email_message",
                    "description": "Send a professional email message to any address.",
                    "parameters": self._props(
                        {"to": "string", "subject": "string", "body": "string"},
                        ["to", "subject", "body"],
                        "E.g. estimate follow-up, thank-you, or contract details.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_deposit_link",
                    "description": "Create a Stripe deposit checkout link for a lead (reserves the project).",
                    "parameters": self._props(
                        {"lead_id": "integer"}, ["lead_id"], "Existing lead id."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_crew",
                    "description": "List crew members (name, role, pay type/rate, active status, direct-deposit / bank status).",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_crew_timesheets",
                    "description": "Look up timesheets for a crew member (hours, status) or a project.",
                    "parameters": self._props(
                        {"crew_id": "integer", "project_id": "integer", "status": "string"},
                        [],
                        "Optional filters: crew_id, project_id, or status (submitted/approved/paid).",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_payroll_summary",
                    "description": "Payroll summary for the current open run: crew, hours/overtime, gross, and paid-vs-pending status (direct deposit via Stripe Connect).",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_payroll",
                    "description": "Run payroll now: finalize the open run and pay all approved lines via Stripe direct deposit.",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_accounting_summary",
                    "description": "Accounting summary: collected deposits, project payments, outstanding / unpaid, and payroll paid total.",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "estimate_materials",
                    "description": "Estimate material quantities + price book (or live API) for a project type and square footage.",
                    "parameters": self._props(
                        {
                            "project_type": "string",
                            "sqft": "number",
                            "include": "array",
                        },
                        ["project_type", "sqft"],
                        "project_type: whole-home, kitchen, bath, roofing, drywall, deck/fence, or handyman. include: optional sku keys (e.g. copper_wire_per_lb, drywall_sheet_1/2).",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_material_price",
                    "description": "Look up a single material price (cents → dollars) from the price book or live API.",
                    "parameters": self._props(
                        {"sku": "string"}, ["sku"], "Sku key, e.g. copper_wire_per_lb."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "search_materials",
                    "description": "Search the brand catalog (Home Depot / Lowe's) by category, brand, store, or free text — returns real products with store item numbers and current prices. Never fabricate a brand or SKU.",
                    "parameters": self._props(
                        {"category": "string", "brand": "string", "query": "string", "store": "string", "limit": "integer"},
                        [],
                        "brand e.g. GAF, Hampton Bay, MOEN, TrafficMaster. query: free-text product lookup.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "sister_business_summary",
                    "description": "Summary report for the sister company (Broom Service — bizstackperks.com STR turnover cleaning): jobs/leads, revenue, crew, payroll, bank status.",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_site_health_check",
                    "description": "Run a health check across both websites (public pages, logins, APIs, Stripe, email/SMS services).",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_training_deck",
                    "description": "Generate a crew orientation PowerPoint training deck. Kinds: construction-trades (Tools of the Trade), construction-safety (Job Site Safety / OSHA-10 baseline), construction-app (Crew App install & time reporting), construction-ethics (Workplace Ethics & Conduct), construction (combined orientation), worker, host.",
                    "parameters": self._props(
                        {"kind": "string"}, ["kind"], "kind: construction-trades, construction-safety, construction-app, construction-ethics, construction, worker, or host."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grade_training_quiz",
                    "description": "Grade a worker's training test (OSHA-10 safety, trade entry tests, or ethics). Returns pass/fail, score, and missed topics for review.",
                    "parameters": self._props(
                        {"crew_id": "integer", "test_slug": "string", "answers": "array"},
                        ["crew_id", "test_slug", "answers"],
                        "test_slug: entry-tape, entry-framing, entry-drywall, entry-roofing, entry-plumbing, entry-electrical, entry-hvac, entry-tile, entry-painting, entry-concrete, or osha/electrician/plumber/roofing/hvac/painting/concrete core tests. answers: list of option indexes or typed strings for tape-measure questions.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "training_status",
                    "description": "Safety + skills training report: which crew members have passed the OSHA-10 safety orientation and trade skills tests, best scores, and who still has outstanding training.",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "remind_crew_training",
                    "description": "Text a reminder to crew who haven't passed the OSHA-10 safety orientation yet (or a single crew member by id), pointing them to the Training tab in the crew phone app.",
                    "parameters": self._props(
                        {"crew_id": "integer"}, [], "Optional single crew id to remind."
                    ),
                },
            },
            # --- Permit lead enrichment -------------------------------------
            # The pipeline for turning a permit lead into a contactable one:
            #   ZIP -> owner name -> email/phone -> save -> draft
            # Each step is cached, so re-running the pipeline is free. The model's
            # web_search is NOT part of this: it is fenced off people-search
            # domains because these leads are homeowners, not businesses.
            {
                "type": "function",
                "function": {
                    "name": "skip_trace_owner",
                    "description": (
                        "Look up the OWNER OF RECORD for a property address from the public "
                        "assessor record. On success the name is in `owner_of_record` (e.g. "
                        "'Blow, Vernon K' -- last, first; take the part after the comma for a "
                        "first-name greeting). Also returns `parcel`, `assessed_value`, "
                        "`year_built` and `is_residential`. It does NOT return an email or a "
                        "phone -- no public record carries those; use pdl_contact for them. "
                        "Cached per address, so a repeat lookup is free."
                    ),
                    "parameters": self._props(
                        {"address": "string"}, ["address"],
                        "Full property address, e.g. '8494 Lynn River Road, Norfolk, VA'.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_zip",
                    "description": (
                        "Resolve the ZIP code for a street address. Free geocoder, no key, no "
                        "quota. Needed because the permit feeds publish the street address but "
                        "not always the ZIP, and the contact lookup refuses to run without one."
                    ),
                    "parameters": self._props(
                        {"address": "string"}, ["address"],
                        "Full street address, e.g. '8494 Lynn River Road, Norfolk, VA'.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "pdl_contact",
                    "description": (
                        "Look up an EMAIL and PHONE for a homeowner address via People Data "
                        "Labs. This is the contact step that skip_trace_owner cannot do. Use "
                        "web_search instead ONLY for businesses -- it is fenced off "
                        "people-search sites. Fetches a ZIP itself if the address lacks one. "
                        "Cached per address and every lookup is audited. The phone is "
                        "RECORDED, never dialled: these are residential lines and calling one "
                        "requires a do-not-call check first."
                    ),
                    "parameters": self._props(
                        {"address": "string", "owner_name": "string"},
                        ["address"],
                        "Full property address, ZIP preferred. owner_name is the name from "
                        "skip_trace_owner, if you already have it.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "save_contact",
                    "description": (
                        "Attach a researched email, and optionally a name and phone, to an "
                        "existing lead. Use this the moment you have a contact -- research that "
                        "is only reported in chat is lost, and the same work gets redone next "
                        "session. Real values are never overwritten. Writes only; sends nothing."
                    ),
                    "parameters": self._props(
                        {"lead_id": "string", "email": "string", "name": "string", "phone": "string"},
                        ["lead_id"],
                        "lead_id from lookup_leads or list_leads -- never invent one. At least "
                        "one of email or phone is required.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "draft_lead_email",
                    "description": (
                        "Write an email DRAFT onto a lead card for the owner to review. Stages "
                        "only -- this cannot send, and the draft waits on the leads page for a "
                        "human to press send. Pass subject and body to write your own wording, "
                        "or leave them blank for the standard first-touch note. Requires the "
                        "lead to already have a real email address."
                    ),
                    "parameters": self._props(
                        {"lead_id": "string", "subject": "string", "body": "string"},
                        ["lead_id"],
                        "lead_id from lookup_leads or list_leads. Blank subject and body use the "
                        "standard note built from the lead's owner name and property address.",
                    ),
                },
            },
        ]
        tools += self._operator_tools()

        # send_email_message is deliberately withheld from the Copilot as of
        # 2026-10-03, and build_copilot_handlers pops the handler to match. The
        # SCHEMA was left behind, so the model was still offered a send tool whose
        # only possible result was "No handler for tool: send_email_message" --
        # burning a tool-loop iteration and reporting a capability that does not
        # exist. Dropping it here completes the withholding.
        #
        # This list is copilot-only. send_email_message lives in _full_tools(),
        # not _safe_tools(), so the public widget never saw it -- and the handler
        # in build_tool_handlers is untouched for the paths that enforce
        # contact_policy_allows (auto_reply, send-once, the digest mails).
        tools = [t for t in tools
                 if t.get("function", {}).get("name") != "send_email_message"]

        # The Maps tools no longer touch Google at all -- they are backed by
        # open_geo (Census/ArcGIS/Nominatim for geocoding, OSRM for routing).
        # The only thing withheld here is maps_find_place, which was removed with
        # its provider: a place database needs a key, and the keyless Overpass
        # instances were unreliable enough to be worse than absent. build_maps_tools
        # withholds the handler at the same moment, so the two stay in step.
        dead_maps = {"maps_find_place"}
        tools = [t for t in tools
                 if t.get("function", {}).get("name") not in dead_maps]

        return tools

    def _tools(self) -> list:
        return self._full_tools() if self._subset == "copilot" else self._safe_tools()

    # --- Tool exec --------------------------------------------------------
    @staticmethod
    def _tool_budget() -> int:
        """How many times ONE tool may run in a single request.

        Two means one call plus one retry. The cap is per tool, not per request:
        the lead pipeline is sequential and needs several different tools, so a
        request-wide cap would break it, while a per-tool cap still stops the
        model grinding one dead call until the proxy kills the request.
        COPILOT_TOOL_ATTEMPTS overrides it.
        """
        try:
            return max(1, int(os.getenv("COPILOT_TOOL_ATTEMPTS") or 2))
        except (TypeError, ValueError):
            return 2

    @staticmethod
    def _tool_deadline() -> float:
        """Wall-clock ceiling on one request, in seconds. COPILOT_TOOL_DEADLINE_SECONDS."""
        try:
            return max(5.0, float(os.getenv("COPILOT_TOOL_DEADLINE_SECONDS") or 90))
        except (TypeError, ValueError):
            return 90.0

    def _execute_tool(self, name: str, arguments: str) -> str:
        handler = self._tool_handlers.get(name)
        if handler is None:
            return json.dumps({"ok": False, "error": f"No handler for tool: {name}"})
        try:
            args = json.loads(arguments or "{}") if isinstance(arguments, str) else (arguments or {})
            result = handler(**args)
            if not isinstance(result, (str, bytes)):
                result = json.dumps(result)
            return result
        except TypeError as e:
            return json.dumps({"ok": False, "error": f"Invalid tool arguments: {e}"})
        except Exception as e:
            print(f"⚠️ Tool {name} failed: {e}")
            return json.dumps({"ok": False, "error": str(e)})

    # --- Conversation loop ------------------------------------------------
    def process_conversation(self, history: list, message: str) -> str:
        """Reply with the full transcript in context.

        This is the memory fix. `process_inbound_text` builds a bare
        `[system, user]` pair, so every turn arrived with no recollection of the
        previous one -- the model would ask something, get the answer, then ask
        what the conversation was about. Here the caller supplies the prior
        turns and they are replayed verbatim, so pronouns and follow-ups resolve
        against what was actually said.
        """
        fallback = "Message received. Our team will follow up with you shortly."
        if not os.getenv("OPENAI_API_KEY"):
            return fallback

        messages: list = [{"role": "system", "content": self._build_system_prompt()}]
        for turn in history or []:
            role = (turn or {}).get("role")
            content = (turn or {}).get("content") or ""
            if role in ("user", "assistant") and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": message})

        if not self._tool_handlers:
            return self._simple_reply(messages)

        max_tokens = 800 if self._subset == "copilot" else 300
        try:
            budget = self._tool_budget()
            deadline = time.monotonic() + self._tool_deadline()
            attempts: dict = {}
            for _ in range(6):
                if time.monotonic() > deadline:
                    raise ToolBudgetExhausted(
                        f"Stopped after {int(self._tool_deadline())}s of tool calls "
                        f"without finishing. Narrow the request to one lead or address."
                    )
                response = self.client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=self._tools(),
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=0.7,
                )
                message_obj = response.choices[0].message
                if not message_obj.tool_calls:
                    return (message_obj.content or "").strip() or fallback

                messages.append(
                    {
                        "role": "assistant",
                        "content": message_obj.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in message_obj.tool_calls
                        ],
                    }
                )
                exhausted = None
                for tc in message_obj.tool_calls:
                    name = tc.function.name
                    attempts[name] = attempts.get(name, 0) + 1
                    if attempts[name] > budget:
                        # Refused, not executed. Every tool_call still needs a
                        # matching tool message or the next API call 400s, so the
                        # refusal is answered in the same slot.
                        exhausted = exhausted or name
                        content = json.dumps({
                            "ok": False,
                            "error": f"{name} already ran {budget} time(s) in this request "
                                     f"without succeeding. Not retrying it again.",
                        })
                    else:
                        content = self._execute_tool(name, tc.function.arguments)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": content}
                    )

                if exhausted:
                    raise ToolBudgetExhausted(
                        f"{exhausted} failed {budget} time(s) in a row, so the request was "
                        f"stopped instead of retrying further. Check that tool's provider "
                        f"credentials or quota -- see the server log for the exact error."
                    )
            return fallback
        except ToolBudgetExhausted:
            raise
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback

    def process_inbound_text(self, context_stream: str) -> str:
        """Handle an inbound SMS/voice message end-to-end (with tool calling if wired).

        Stateless by design: SMS and voice callers arrive as isolated messages.
        The Copilot and the web widget use `process_conversation` instead.
        """
        fallback = "Message received. Our team will follow up with you shortly."
        if not os.getenv("OPENAI_API_KEY"):
            return fallback

        if not self._tool_handlers:
            return self._simple_reply(
                [
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": context_stream},
                ]
            )

        messages: list = [
            {"role": "system", "content": self._build_system_prompt()},
            {"role": "user", "content": context_stream},
        ]

        max_tokens = 800 if self._subset == "copilot" else 300
        try:
            for _ in range(6):
                response = self.client.chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=self._tools(),
                    tool_choice="auto",
                    max_tokens=max_tokens,
                    temperature=0.7,
                )
                message = response.choices[0].message
                if not message.tool_calls:
                    return (message.content or "").strip() or fallback

                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "id": tc.id,
                                "type": "function",
                                "function": {
                                    "name": tc.function.name,
                                    "arguments": tc.function.arguments,
                                },
                            }
                            for tc in message.tool_calls
                        ],
                    }
                )
                for tc in message.tool_calls:
                    tool_output = self._execute_tool(tc.function.name, tc.function.arguments)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_output,
                        }
                    )
            return fallback
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback

    def _simple_reply(self, messages: list) -> str:
        """No tools wired: single model call over the prepared message list."""
        fallback = "Message received. Our team will follow up with you shortly."
        try:
            response = self.client.chat.completions.create(
                model=self._model,
                messages=messages,
                max_tokens=800 if self._subset == "copilot" else 300,
                temperature=0.7,
            )
            return response.choices[0].message.content or fallback
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback
