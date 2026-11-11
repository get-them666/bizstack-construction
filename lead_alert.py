"""Instant email alerts when a new lead comes in.

Why this exists
---------------
The owner needs to know the moment a homeowner submits a form, so they can call
back before the lead goes cold. This module sends a plain-text email to the
owner's inbox with the lead's name, phone, project type, and goal.

It uses aiosmtplib (already a dependency) so it shares the same transport
(Gmail / Resend / SMTP) as the rest of the app. Falls back to printing a log
line when no transport is configured, so a missing SMTP password never silently
drops an alert.
"""
from __future__ import annotations

import os
import asyncio
from datetime import datetime, timezone


def send_lead_alert(
    *,
    lead_id: int,
    name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
    project_type: str = "",
    goal: str = "",
    budget: str = "",
    timeline: str = "",
    message: str = "",
    source: str = "website",
) -> bool:
    """Fire-and-forget lead alert. Returns True if the email was dispatched.

    Runs the coroutine on a throwaway event loop so it can be called from
    anywhere (sync route handler, background thread, etc.)."""
    targets = _owner_targets()
    if not targets:
        print(f"[lead-alert] no owner email configured; alert for lead #{lead_id} skipped", flush=True)
        return False

    subject = f"New lead #{lead_id}: {name or 'New inquiry'}"
    body = _format_alert(
        lead_id=lead_id, name=name, phone=phone, email=email,
        address=address, project_type=project_type, goal=goal,
        budget=budget, timeline=timeline, message=message, source=source,
    )

    ok = False
    for to in targets:
        try:
            result = _run_coro(_send_email(to, subject, body))
            if result:
                ok = True
                print(f"[lead-alert] emailed {to} (lead #{lead_id})", flush=True)
            else:
                print(f"[lead-alert] email to {to} not delivered", flush=True)
        except Exception as exc:
            print(f"[lead-alert] email to {to} failed: {exc}", flush=True)
    return ok


def _format_alert(*, lead_id, name, phone, email, address, project_type,
                  goal, budget, timeline, message, source) -> str:
    lines = [
        f"New lead #{lead_id} from {source}",
        f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    if name:
        lines.append(f"Name: {name}")
    if phone:
        lines.append(f"Phone: {phone}")
    if email:
        lines.append(f"Email: {email}")
    if address:
        lines.append(f"Address: {address}")
    if project_type:
        lines.append(f"Project: {project_type}")
    if goal:
        lines.append(f"Goal: {goal}")
    if budget:
        lines.append(f"Budget: {budget}")
    if timeline:
        lines.append(f"Timeline: {timeline}")
    if message:
        lines.append("")
        lines.append(f"Message: {message[:500]}")
    lines.append("")
    lines.append("---")
    lines.append(f"View all leads: https://construction.bizstackperks.com/admin/leads")
    lines.append(f"Call back now: the first GC to respond wins the job.")
    return "\n".join(lines)


def _owner_targets() -> list:
    """Owner/admin mailbox list: NOTIFY_EMAIL, else ADMIN_EMAIL."""
    notify = (os.getenv("NOTIFY_EMAIL") or "").strip()
    if notify:
        return [t.strip() for t in notify.split(",") if t.strip()]
    admin = (os.getenv("ADMIN_EMAIL") or "").strip()
    if admin:
        return [admin]
    return ["hello@bizstackperks.com"]


def _run_coro(coro) -> bool:
    """Run an async coroutine from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Already in an async context — run in a new thread to avoid blocking.
        import threading
        result = {"value": None, "error": None}
        def _runner():
            try:
                result["value"] = asyncio.run(coro)
            except Exception as e:
                result["error"] = e
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=30)
        if result["error"]:
            raise result["error"]
        return result["value"]
    else:
        return asyncio.run(coro)


async def _send_email(to: str, subject: str, body: str) -> bool:
    """Send via aiosmtplib using the app's configured transport.

    Reads SMTP/Gmail config from the environment — same path as documents_service."""
    import documents_service
    cfg = documents_service.smtp_config_from_env()
    if not documents_service.smtp_configured(cfg):
        print("[lead-alert] SMTP not configured; cannot send alert", flush=True)
        return False
    # Build a plain-text MIME message.
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["From"] = cfg.get("from", "hello@bizstackperks.com")
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        delivered = await documents_service.send_email(cfg, to, subject, body.replace("\n", "<br>"))
        return bool(delivered)
    except Exception as exc:
        print(f"[lead-alert] send failed: {exc}", flush=True)
        return False
