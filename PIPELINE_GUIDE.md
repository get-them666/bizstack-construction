## Bizstack Construction: Event-Driven Lead Pipeline

### What Changed

Your backend was a **disconnected collection of modules** where data would sit stale and nothing flowed automatically. Now it's a **cascading event system** where each event triggers the next one.

---

### The Flow (Fully Automated)

```
1. LEAD CREATED
   ↓
2. Auto-ENRICHED (if address → PDL reverse lookup)
   ↓
3. Auto-CONTACTED (has phone/email now)
   ↓
4. Auto-SMS sent (first outreach)
   ↓
5. Auto-QUOTED (draft quote created)
   ↓
6. Ready for Owner Review
   ↓
7. Owner accepts quote
   ↓
8. Auto-PROJECT created
   ↓
9. Deposit payment link ready
```

**No manual intervention needed.** Lead to project is automatic.

---

### New Files

**`lead_pipeline.py`** — The orchestrator
- `on_lead_created(db, lead_id)` — Entry point when a lead is created
- `on_lead_enriched(db, lead_id)` — Auto-cascade to contact
- `on_lead_contacted(db, lead_id)` — Auto-cascade to quote
- `on_lead_quoted(db, lead_id)` — Ready for owner review
- `on_quote_accepted(db, quote_id)` — Auto-cascade to project creation
- `on_bid_won(db, bid_id)` — Auto-cascade bid to project
- Event logging with audit trail for debugging

**`outreach_queue.py`** — Automatic SMS/email respecting contact policies
- Auto-sends first SMS when lead becomes contacted
- Respects "don't spam" policies (5 days between SMS, 7 days between email)
- Contact policy table in comms_logs for tracking

---

### Integration Points in `construction_main.py`

**1. Lead Creation (submit-lead endpoint)**
```python
await asyncio.to_thread(lead_pipeline.on_lead_created, db, lead_id)
# Cascades: enrich → contact → quote automatically
```

**2. Quote Acceptance (new endpoint: /api/quotes/{quote_id}/accept)**
```python
lead_pipeline.on_quote_accepted(db, quote_id)
# Cascades: create project, move lead to deposit_pending
```

**3. Bid Won (new endpoint: /api/bids/{bid_id}/won)**
```python
lead_pipeline.on_bid_won(db, bid_id)
# Cascades: create project linked to bid
```

**4. Manual Trigger (existing endpoint: /api/leads/{lead_id}/pipeline-trigger)**
```python
await asyncio.to_thread(lead_pipeline.on_lead_created, db, lead_id)
# For re-processing or triggering on demand
```

---

### Database Schema Additions

**`lead_pipeline_events` table**
- Audit trail of every event
- Links events for debugging ("this lead enriched, then contacted, then quoted")
- `cascade_triggered` flag shows which events auto-cascaded

---

### Money Flow

1. **Lead submitted → enriched → quoted automatically** (no owner action yet)
2. **Owner reviews draft quote** (owner sees it on dashboard)
3. **Owner clicks "Accept Quote"**
4. **→ Automatically:**
   - Project created (linked to lead)
   - Lead moved to `deposit_pending` status
   - Quote marked `accepted`
5. **Owner clicks "Send Deposit Link"**
6. **Customer pays deposit → Stripe webhook**
7. **Lead moves to `in_progress`**
8. **Project is now active with paid deposit**

---

### Contact Policy (No Spam)

`outreach_queue.py` enforces:
- SMS: Don't text same number twice in 5 days
- Email: Don't email same address twice in 7 days
- Call: Don't call twice in 14 days

Checked against `comms_logs` before sending. Respects history of all touches.

---

### What Happens to Stale Data

**Before:**
- Lead sits with no contact → never moves forward → money never made
- Permits imported but never enriched → owner manually runs PDL → slow
- Quote created but owner never notices → no project → no payment

**After:**
- Lead created → immediately enriched → contacted → quoted within seconds
- Permit imported → auto-enriched → auto-contacted
- Quote auto-created → owner sees it → accepts → project created → payment ready
- Contact history tracked so no spam

---

### Testing the Flow

**Manual trigger (e.g., lead #42):**
```bash
curl -X POST http://localhost:8000/api/leads/42/pipeline-trigger \
  -H "Authorization: Bearer YOUR_TOKEN"
```

**Check event trail:**
```sql
SELECT event_type, details, created_at 
FROM lead_pipeline_events 
WHERE lead_id = 42 
ORDER BY created_at;
```

---

### Key Differences from "Connected"

| Before | After |
|--------|-------|
| Lead created → sits | Lead created → enriched → contacted → quoted (auto) |
| Enrichment manual batch job | Enrichment automatic on lead create |
| No auto-qualify | Auto-qualify when contact info exists |
| Quote manual | Quote auto-generated on contact |
| Project manual | Project auto-created on quote accept |
| No SMS queuing | Auto-SMS on contact with policy enforcement |
| Stale data/unclear transitions | Event log shows exact cascade path |

---

### Configuration

All auto-triggers work if:
- **PDL API key set** (for enrichment) → `PDL_API_KEY` env var
- **SignalWire credentials set** (for SMS) → `SIGNALWIRE_PHONE` env var
- **Postgres database** with created schema

Each event fails gracefully and logs. A missing API key doesn't break the flow—it just skips that step.

---

### Next Steps (Optional Enhancements)

1. **Web hooks** — Notify owner immediately when lead becomes quoted
2. **Deposit link auto-send** — Email deposit link when quote accepted
3. **Project assignment** — Auto-assign crew based on project type
4. **Automatic invoicing** — Create invoice on project.in_progress
5. **AI copilot hook** — Let copilot monitor and nudge owner if quotes sitting unreviewed
