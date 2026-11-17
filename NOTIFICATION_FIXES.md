# Notification Failure Visibility Fixes

**Date:** 2026-11-17  
**Problem:** Email and SMS notifications were silently failing due to transport issues (Zoho SMTP timeout, expired Gmail OAuth, empty ZeptoMail token, SignalWire inactive_campaign).  
**Solution:** Make every failure VISIBLE in logs, queue failed emails for retry, and add daily health checks.

---

## Task 1: Wrap all notification calls in try/except + logging

### construction_main.py

**submit-lead route (lines 3516-3537):**
- Split `leads_db.insert_lead` and `lead_alert.send_lead_alert` into separate try/except blocks
- Each now has its own `[lead-notify]` or `[lead-alert]` prefix
- SQLite mirror failure no longer prevents lead alert from running
- All notification calls now log with lead_id for traceability

**submit-lead route (lines 3573-3595):**
- Added done callback to `tidy_service.enrich_and_sync_tidy` create_task
- Task-internal exceptions now surface as `[lead-notify] tidy enrichment failed` instead of "Task exception was never retrieved"

**instant-quote route (lines 10247-10296):**
- Split `leads_db.insert_lead` and `lead_alert.send_lead_alert` into separate try/except blocks
- Separated `auto_reply.notify_owner_email` from tidy `create_task`
- Added done callback to tidy create_task
- All notification calls now use `[lead-notify]` or `[lead-alert]` prefix with lead_id

**finance route (line 3902):**
- Wrapped `zoho_sync.after_intake` in try/except (was unprotected)
- Now logs `[finance] zoho_sync.after_intake failed for lead {lead_id}`

**embed/quote route (line 10313):**
- Wrapped `zoho_sync.after_intake` in try/except (was unprotected)
- Now logs `[lead-notify] embed quote zoho_sync.after_intake failed`

---

## Task 2: Enhance lead_alert.py to log failures visibly

### lead_alert.py

**send_lead_alert (lines 21-62):**
- Line 41: Changed log prefix to `⚠️ [lead-alert]` with `⚠️` emoji
- Line 59: Enhanced "not delivered" message to include `(transport returned False)`
- Line 61: Enhanced exception log to include lead_id
- Line 141: Enhanced SMTP-not-configured message to include lead_id

### auto_reply.py

**_notify_owner_sms (lines 148-151):**
- Now checks `sw.send_sms()` return value
- Logs `⚠️ [lead-alert] SMS to owner failed (SignalWire returned False)` when send_sms returns False
- Previously printed "SMS alert sent" unconditionally, even on failure

---

## Task 3: Add daily notification-health summary

### construction_main.py

**_notification_health_check (lines 466-499):**
- Logs transport status once daily (SMTP configured, ZeptoMail token, EMAIL_TRANSPORT, SignalWire credentials, OWNER_MOBILE, NOTIFY_EMAIL)
- Calls `retry_failed_emails()` to attempt resending queued failures
- Runs once at boot + once every 24 hours

**_notification_health_loop (lines 981-992):**
- Background thread started in lifecycle()
- Calls `_notification_health_check()` at boot, then every 24h
- Isolated in its own thread so failures don't affect the app

---

## Task 4: Queue failed notifications for retry

### documents_service.py

**queue_failed_email (lines 26-52):**
- Appends failed email to `/tmp/failed_emails.jsonl`
- Records: to, subject, body, reason, attempted_at, from
- Called when send_email returns False or raises (except for policy blocks like bank details)

**retry_failed_emails (lines 55-108):**
- Reads `/tmp/failed_emails.jsonl`
- Attempts to resend each email via `send_email()`
- Removes successful sends from queue
- Returns count of retried emails
- Called by `_notification_health_check()` daily

**send_email modifications (lines 1051, 1174):**
- Line 1051: Queues email when Gmail unavailable + no fallback enabled
- Line 1174: Queues email when all SMTP ladder attempts fail (before raising)
- Policy blocks (bank details at line 803) do NOT queue

---

## Verification

All changes compile successfully:
```bash
venv/bin/python -m py_compile construction_main.py
venv/bin/python -m py_compile lead_alert.py
venv/bin/python -m py_compile documents_service.py
venv/bin/python -m py_compile auto_reply.py
# ALL OK
```

Import test passes:
```bash
DATABASE_URL=postgresql://dummy@localhost:5432/dummy venv/bin/python -c "
import lead_alert
import documents_service
print('queue_failed_email:', hasattr(documents_service, 'queue_failed_email'))
print('retry_failed_emails:', hasattr(documents_service, 'retry_failed_emails'))
"
# ALL OK
```

Retry queue end-to-end test passes:
- Queue file created at `/tmp/failed_emails.jsonl`
- Failed emails appended with reason
- `retry_failed_emails()` attempts resend
- Queue preserved when SMTP not configured

---

## What's NOT fixed (requires user action)

1. **Zoho SMTP timeout (port 587 blocked on DigitalOcean)**
   - Fix: Configure ZeptoMail token or refresh Gmail OAuth
   - Symptom: `[EMAIL] zoho path failed` in logs

2. **Gmail OAuth token expired**
   - Fix: Refresh OAuth token via Google Cloud Console
   - Symptom: `[EMAIL] Gmail transport unavailable` in logs

3. **ZeptoMail token empty**
   - Fix: Set `ZEPTOMAIL_TOKEN` environment variable
   - Symptom: `[transport-health] ZEPTOMAIL_TOKEN set: False` in daily health check

4. **SignalWire inactive_campaign**
   - Fix: Register 10DLC campaign in SignalWire dashboard
   - Symptom: `⚠️ [lead-alert] SMS to owner failed (SignalWire returned False)` in logs

---

## What IS fixed

✅ Every notification failure now logs with `[lead-notify]` or `[lead-alert]` prefix  
✅ Every log includes lead_id for traceability  
✅ asyncio.create_task exceptions now surface via done callbacks  
✅ Failed emails queued to `/tmp/failed_emails.jsonl` for retry  
✅ Daily health check logs transport status and retries failed emails  
✅ SMS failures now check return value instead of printing false success  
✅ No 500 errors from notification failures (all wrapped in try/except)  

---

## Log patterns to watch for

```bash
# Daily health check (once per day)
[transport-health] SMTP configured: False
[transport-health] ZEPTOMAIL_TOKEN set: False
[transport-health] EMAIL_TRANSPORT: gmail
[transport-health] SIGNALWIRE_PROJECT_ID set: True
[transport-health] OWNER_MOBILE set: True

# Notification failures
⚠️ [lead-alert] email alert failed for lead 123: SMTP not configured
⚠️ [lead-notify] tidy enrichment failed for lead 123: ConnectionError(...)
⚠️ [lead-alert] SMS to owner failed (SignalWire returned False) for lead 123

# Retry queue
[email-retry] queued failed email to owner@example.com: all transports unavailable
[email-retry] retried 3 failed email(s) from queue
```
