import json
import os
from datetime import datetime
from typing import Optional

from openai import OpenAI


class BusinessAIAgent:
    """Conversational assistant that runs Broom Service.

    Two personalities share this class:
    - ``subset="guest"``  -> the public SMS/voice assistant (safe guest toolkit).
    - ``subset="copilot" -> the owner's private operator chat with the FULL toolkit
      (clients, scheduling, payroll, accounting, comms, training, diagnostics).

    Both use the same knowledge base, but the copilot may change the database because
    only the owner can reach it.
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
            "You are the automated representative for Broom Service, a short-term "
            "rental turnover cleaning and co-hosting company. Be concise and professional."
        )
        if self._subset == "copilot":
            identity = (
                "You are the Broom Service OPERATOR COPILOT for the owner.\n"
                "You are acting with full authority over the owner's business database.\n"
                "You can add clients, manage workers and schedules, run payroll, review "
                "accounting, send SMS/email, generate training decks, run rental "
                "analyses, check site health, and summarize business performance.\n"
                "SAFETY: Before making any database CHANGE (not a read), restate the "
                "change in one short line and confirm with the owner first. Reads are free.\n"
                "SKILLS: You have hospitality, Airbnb/STR math, payroll and accounting "
                "expertise. Explain things simply when the owner asks."
            )
        else:
            identity = (
                "You are the automated public assistant for Broom Service.\n"
                "Guests and leads text/call you. You check availability, create guest "
                "bookings, look up bookings, register prospects, and answer questions "
                "from the knowledge base. NEVER expose internal business data.\n"
                "If someone asks you to change pricing, delete bookings, or access "
                "payroll or other customers' data, politely decline and say the team "
                "will follow up."
            )

        return f"""
{identity}
Current time: {now}.
You must answer strictly from the knowledge base below. Never invent prices,
availability, policies, or URLs.

HOW TO SOUND LIKE A REAL HUMAN (non-negotiables):
- Write the way a friendly, sharp human assistant texts: contractions, short punchy
  sentences, and a natural rhythm. Vary sentence length. Never sound robotic, canned,
  or like a script.
- Open naturally based on context: a quick hey, a friendly confirmation, or a warm
  "Got it —". No "Greetings!", no "As an AI", no corporate boilerplate.
- One thought per text: break things into short lines instead of giant walls of text.
  Keep total replies short — a guest reads these on a phone.
- Use light, human details ("Perfect — Friday at 2 works!"), but stay truthful. Never
  invent facts. If you don't know, say so plainly ("Let me confirm that for you.").
- It's fine to be warm and a little personality-driven, but never over-the-top, never
  use 10 emojis, and never pretend to be a specific named person. You are the Broom Service
  Hosts assistant.
- When confirming a booking, mirror real human confirmation style: restate the
  details simply and tell them exactly what happens next ("I've got you locked in for
  Friday at 2 — here's your secure payment link to confirm").

KNOWLEDGE BASE:
{knowledge}

TOOL USAGE RULES:
- Use check_booking_availability before suggesting or confirming any time slot.
- Use create_booking ONLY after the guest has confirmed name, service, date, and time.
- Always follow a successful create_booking by giving the guest the payment link.
- Use lookup_bookings whenever a guest asks about their existing bookings.
- Use register_customer when a new guest/prospect shares their info and you have no
  existing booking for them yet.
- Use get_business_summary when the owner asks how the business is doing.
- If a tool returns an error or an unavailable slot, respond helpfully and offer
  the nearest open time from the availability result.
- When checking availability or creating bookings, you MUST convert the guest's
  requested date/time to an ISO-8601 local datetime string like 2026-09-18T14:00:00.
  Assume US Eastern time (America/New_York) when the guest gives a date without a zone.
- Copilot: money amounts are passed as integer dollars ("pay_rate_dollars",
  "amount_dollars"); dates as YYYY-MM-DD. Worker pay rate and job counts are always
  stored as cents internally — the tools convert for you.
- Copilot: for payroll, first list_workers to confirm the worker exists, then
  generate_paycheck. For accounting, get_accounting_summary to report numbers.
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
                    "name": "check_booking_availability",
                    "description": (
                        "Check whether a requested start time is open for a cleaning "
                        "operation. Returns open/conflict status and the nearest open "
                        "slots around the requested time."
                    ),
                    "parameters": self._props(
                        {"start_time": "string"},
                        ["start_time"],
                        "ISO-8601 local datetime, e.g. 2026-09-18T14:00:00.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_booking",
                    "description": (
                        "Create a confirmed booking and generate the guest's secure Stripe "
                        "payment link. ONLY use after the guest has explicitly confirmed "
                        "their name, service type, date, and time."
                    ),
                    "parameters": self._props(
                        {
                            "customer_name": "string",
                            "phone": "string",
                            "service_type": "string",
                            "start_time": "string",
                        },
                        ["customer_name", "phone", "service_type", "start_time"],
                        "service_type: one of Turnover Cleaning, Deep Cleaning, "
                        "Linen Restock, Inspection.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lookup_bookings",
                    "description": "Look up a guest's bookings by phone number, newest first.",
                    "parameters": self._props(
                        {"phone": "string"}, ["phone"], "Phone used for the booking."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "register_customer",
                    "description": "Save a new customer or prospect record with their contact details.",
                    "parameters": self._props(
                        {"name": "string", "email": "string", "phone": "string"},
                        ["name"],
                        "Customer name; email/phone optional if provided.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_business_summary",
                    "description": "Return current business stats: hosts, customers, bookings, and leads counts.",
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
                    "name": "list_upcoming_schedule",
                    "description": "List upcoming scheduled bookings for the next N days, with worker assignments and payment status.",
                    "parameters": self._props({"days": "integer"}, [], "Defaults to 7."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_customers",
                    "description": "List customers, optionally filtered by a name/phone/email search.",
                    "parameters": self._props({"search": "string"}, [], "Search term, optional."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_leads",
                    "description": "List leads (potential hosts/customers from the analysis form), optionally by status.",
                    "parameters": self._props({"status": "string"}, [], "Optional status filter."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_lead_status",
                    "description": "Change a lead's pipeline status (e.g. new, contacted, funded, converted, lost).",
                    "parameters": self._props(
                        {"lead_id": "integer", "status": "string"}, ["lead_id", "status"], ""
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_host",
                    "description": "Add a new host client (property owner) to the business.",
                    "parameters": self._props(
                        {
                            "name": "string",
                            "email": "string",
                            "phone": "string",
                            "property_name": "string",
                        },
                        ["name"],
                        "Only name required; others optional.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_hosts",
                    "description": "List all host clients.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_worker",
                    "description": "Add a new worker to the crew.",
                    "parameters": self._props(
                        {
                            "name": "string",
                            "phone": "string",
                            "email": "string",
                            "pay_rate_dollars": "number",
                        },
                        ["name"],
                        "pay_rate_dollars is hourly/job rate in dollars, optional.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_workers",
                    "description": "List the crew.",
                    "parameters": self._props({"active_only": "boolean"}, [], "Defaults to true."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "update_worker",
                    "description": "Update a worker's details (name, phone, email, pay rate, active flag).",
                    "parameters": self._props(
                        {
                            "worker_id": "integer",
                            "name": "string",
                            "phone": "string",
                            "email": "string",
                            "pay_rate_dollars": "number",
                            "is_active": "boolean",
                        },
                        ["worker_id"],
                        "Only supplied fields are updated.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "assign_worker_to_job",
                    "description": "Assign a worker to a scheduled booking/job.",
                    "parameters": self._props(
                        {"event_id": "integer", "worker_id": "integer"},
                        ["event_id", "worker_id"],
                        "",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_worker_jobs",
                    "description": "List a worker's assigned jobs over the next N days.",
                    "parameters": self._props(
                        {"worker_id": "integer", "days": "integer"}, ["worker_id"], ""
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_paycheck",
                    "description": "Generate a worker's paycheck for a date range and create the matching ledger expense.",
                    "parameters": self._props(
                        {
                            "worker_id": "integer",
                            "period_start": "string",
                            "period_end": "string",
                        },
                        ["worker_id", "period_start", "period_end"],
                        "Dates as YYYY-MM-DD.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_paychecks",
                    "description": "List issued paychecks, optionally for one worker.",
                    "parameters": self._props({"worker_id": "integer"}, [], "Optional worker filter."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_accounting_summary",
                    "description": "Revenue, expenses, balance and recent ledger entries.",
                    "parameters": self._props({"period_days": "integer"}, [], "Default 30."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "add_ledger_entry",
                    "description": "Manually record a ledger entry (revenue or expense).",
                    "parameters": self._props(
                        {
                            "tx_type": "string",
                            "description": "string",
                            "amount_dollars": "number",
                        },
                        ["tx_type", "description", "amount_dollars"],
                        "tx_type: revenue or expense.",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_sms_message",
                    "description": "Send an outbound SMS text message from the Broom Service number to a phone.",
                    "parameters": self._props(
                        {"to": "string", "body": "string"}, ["to", "body"], "E.164 format phone."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "send_email_message",
                    "description": "Send an outbound email from hello@bizstackperks.com.",
                    "parameters": self._props(
                        {"to": "string", "subject": "string", "body": "string"},
                        ["to", "subject", "body"],
                        "",
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "run_site_health_check",
                    "description": "Check site health: DB connectivity, key config, and integrations. Reports what is broken and how to fix.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_training_deck",
                    "description": "Generate a PowerPoint training deck (worker orientation or host/lead onboarding) and save it.",
                    "parameters": self._props(
                        {"kind": "string"}, ["kind"], "kind: worker or host."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_rental_analysis",
                    "description": "Run a data-backed rental earnings analysis for a property address.",
                    "parameters": self._props(
                        {"address": "string"}, ["address"], "Full property address."
                    ),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_documents",
                    "description": "List generated documents (contracts, invoices, training decks) in the library.",
                    "parameters": self._props({"category": "string"}, [], "Optional category filter."),
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "list_funding_ready_leads",
                    "description": "List leads flagged as needing capital/funding for bank partner referral.",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        return tools

    def _tools(self) -> list:
        return self._full_tools() if self._subset == "copilot" else self._safe_tools()

    # --- Tool exec --------------------------------------------------------
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