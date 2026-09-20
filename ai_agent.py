"""Buildstack Construction — AI business agent (public assistant + owner copilot).

One class, two personalities:

- ``subset="guest"``    -> public SMS + voice assistant (safe tools only)
- ``subset="copilot"``  -> the owner's private operator copilot (full toolkit:
  leads pipeline, permits & city codes, crew, timesheets, payroll + direct deposit,
  accounting, materials estimate, sister-company Broom Service summary, email/SMS,
  site health check, training-deck + OSHA-10 quiz generator, sister business summary).

Both companies run on this same agent: **Buildstack Construction Co.** (this site,
bizstackconstruction.com) and its sister company **Broom Service** (bizstackperks.com,
short-term-rental turnover cleaning + co-hosting). Same owner, two websites, one
copilot brain. When a caller is clearly a Broom Service customer, hand them off; when
the owner asks about the business overall, summarize BOTH.
"""

import json
import os
from datetime import datetime
from typing import Optional

from openai import OpenAI


class BusinessAIAgent:
    """Conversational assistant for Buildstack Construction Co.

    Two personalities share this class:
    - ``subset="guest"``   -> the public SMS/voice assistant (records leads,
      answers questions from the knowledge base).
    - ``subset="copilot"`` -> the owner's private operator chat with the full
      toolkit (leads pipeline, status changes, payroll, crew, permits, materials,
      accounting, sister company, email, health check, training).

    Both use the same knowledge base (bot_knowledge.md), which covers BOTH companies
    (Buildstack Construction + Broom Service). Only the owner can reach the copilot.
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

    def _build_system_prompt(self) -> str:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        knowledge = self._knowledge or (
            "You are the automated representative for Buildstack Construction Co., "
            "a licensed general contractor doing whole-home renovations and repairs. "
            "Be concise and professional."
        )
        if self._subset == "copilot":
            identity = (
                "You are the Buildstack Construction OPERATOR COPILOT for the owner.\n"
                "You act with full authority over the owner's business database. You can "
                "review and update the lead pipeline, summarize business performance, run "
                "payroll + direct deposit, look up permits & city codes, estimate materials, "
                "review accounting, and summarize the sister company (Broom Service).\n"
                "Rule: READs are free. Before any database CHANGE, restate the change in "
                "one short line and confirm with the owner first.\n"
                "You also know about Broom Service (bizstackperks.com) — the sister "
                "short-term-rental turnover-cleaning company. Use sister_business_summary "
                "to report on it. Never expose tenant or guest data to the public assistant."
            )
        else:
            identity = (
                "You are the automated public assistant for Buildstack Construction Co., "
                "a licensed general contractor serving Hampton Roads, VA and the "
                "Currituck/Elizabeth City, NC area.\n"
                "Callers and texters are homeowners, investors, and short-term-rental "
                "hosts. Capture their project details as a lead, answer questions from "
                "the knowledge base, and offer a free on-site estimate. NEVER expose "
                "internal business data. If someone asks you to change pricing, delete "
                "leads, or access records, politely decline and say the team will follow up."
            )

        return f"""
{identity}

Today's date/time: {now}

## KNOWLEDGE BASE (both companies — Buildstack Construction + Broom Service)
Answer strictly from the knowledge below. Never invent prices, availability, policies,
schedules, or URLs. For exact project pricing, always offer a free on-site estimate
rather than quoting a final number. If the person is a Broom Service customer, hand
them off to the sister company's process and say you'll connect them.

{knowledge}

## Tool rules
Use tools to take action. register a lead right away when you have name + phone. Use
lookup_leads for repeat contact. Never expose internal database rows to the public
assistant. For the owner copilot, use the payroll/crew/materials/permit/accounting/sister
tools with the owner's explicit permission before destructive changes.
"""

    # --- Tool schema helpers ------------------------------------------------
    def _props(self, names_types: dict, required: list, description: str) -> dict:
        return {
            "type": "object",
            "properties": {k: {"type": t} for k, t in names_types.items()},
            "required": required,
            "description": description,
        }

    # --- Tool lists --------------------------------------------------------
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
                        "Flooring, Deck, Fence, STR Make-Ready.",
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
                    "name": "lookup_permits",
                    "description": "Look up a permit by address/parcel, pull city + county permit info from the job-leads / permit record, or search which cities issue new permits (Shovels feed).",
                    "parameters": self._props(
                        {"city": "string", "address": "string"},
                        [],
                        "Optional city filter (e.g. Williamsburg, Newport News, Elizabeth City) or address for an exact permit lookup.",
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
                    "name": "sister_business_summary",
                    "description": "Summary report for the sister company (Broom Service — bizstackperks.com STR turnover cleaning): jobs/leads, revenue, crew, payroll, bank status.",
                    "parameters": self._props({}, [], ""),
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
                    "name": "run_site_health_check",
                    "description": "Run a health check across both websites (public pages, logins, APIs, Stripe, email/SMS services).",
                    "parameters": self._props({}, [], ""),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_training_deck",
                    "description": "Generate an OSHA-10 / orientation / safety / HR / sexual-harassment / trades-knowledge training deck (worker on-boarding) as a PowerPoint and save it.",
                    "parameters": self._props(
                        {"kind": "string"}, ["kind"], "kind: worker."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "grade_training_quiz",
                    "description": "Grade a worker's OSHA-10 / orientation quiz; returns pass/fail, score, and missed topics for review.",
                    "parameters": self._props(
                        {"crew_id": "integer", "answers": "array"},
                        ["crew_id", "answers"],
                        "answers: list of {question_id, answer} dicts. Topics include tape-measure reading, simple math, basic electrical, framing/drywall/roofing/tile/plumbing basics.",
                    ),
                },
            },
        ]
        return tools

    def _tools(self) -> list:
        return self._full_tools() if self._subset == "copilot" else self._safe_tools()

    # --- Tool execution ----------------------------------------------------
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

    # --- Conversation loop -------------------------------------------------
    def process_inbound_text(self, context_stream: str) -> str:
        """Handle an inbound SMS/voice message end-to-end (with tool calling if wired)."""
        fallback = "Message received. Our team will follow up with you shortly."
        if not os.getenv("OPENAI_API_KEY"):
            return fallback

        if not self._tool_handlers:
            return self._simple_reply(context_stream)

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

    def _simple_reply(self, context_stream: str) -> str:
        fallback = "Message received. Our team will follow up with you shortly."
        try:
            response = self.client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": self._build_system_prompt()},
                    {"role": "user", "content": context_stream},
                ],
                max_tokens=800 if self._subset == "copilot" else 300,
                temperature=0.7,
            )
            return response.choices[0].message.content or fallback
        except Exception as e:
            print(f"⚠️ AI agent fallback triggered: {e}")
            return fallback
