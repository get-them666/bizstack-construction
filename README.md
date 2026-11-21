# BizStack Quote Engine — Real-Time Scheduling & AI Vision Quote System

> **Modular Python + HTML upgrade for BuildStack Construction & Broom Service**
> Photo-powered instant quotes, Cal.com scheduling, Zelle/ACH deposits — zero Stripe.

---

## Overview

This is a standalone FastAPI application that provides:

1. **AI Vision Quote Engine** — Customers upload photos → Gemini/OpenAI analyzes → deterministic Python pricing
2. **Real-Time Scheduling** — Cal.com embed widget with pre-filled customer data
3. **Deposit Instructions** — Zelle & ACH payment flow with automated emails
4. **Admin Portal** — Quote management, pricing rules editor, marketing dashboard
5. **Local SEO** — Programmatic city landing pages, JSON-LD schema, review funnel, social posting
6. **Review Funnel** — 5-star gate routing positive reviews to Google/Facebook/Yelp, negative feedback to owner

### Domains
- **BuildStack Construction** (`construction.bizstackperks.com`) — General Contractor
- **Broom Service** (`bizstackperks.com`) — STR Turnover Cleaning & Co-Hosting

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     FastAPI Application                          │
├─────────────────────────────────────────────────────────────────┤
│  main.py           → 27 routes (quotes, scheduling, admin, SEO) │
│  models.py         → SQLAlchemy ORM (7 tables)                  │
│  schemas.py        → Pydantic structured outputs                │
│  services/                                                          │
│    ├── vision_analyzer.py  → Gemini + OpenAI dual-provider      │
│    ├── pricing_engine.py   → Deterministic pricing rules        │
│    ├── email_service.py    → SMTP deposit + feedback emails     │
│    └── local_seo.py        → City data, JSON-LD schema          │
│  templates/                                                         │
│    ├── quote_form.html     → 3-step wizard (HTMX + Uppy.js)    │
│    ├── schedule.html       → Cal.com inline embed               │
│    ├── deposit_instructions.html → Zelle/ACH instructions       │
│    ├── review_gate.html    → 5-star review funnel               │
│    ├── city_landing.html   → Programmatic SEO pages             │
│    └── admin/              → Dashboard, pricing, marketing      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Quick Start (Local Development)

```bash
# 1. Install dependencies
pip3 install -r requirements-quote-engine.txt

# 2. Set environment variables (or copy .env.example)
export DATABASE_URL="sqlite:///./quote_engine.db"
export GEMINI_API_KEY="your-key-here"  # or OPENAI_API_KEY

# 3. Run the app
python3 main.py

# 4. Open in browser
open http://localhost:8000/instant-quote?brand=construction
open http://localhost:8000/instant-quote?brand=broom
open http://localhost:8000/admin/quotes
open http://localhost:8000/docs
```

---

## Route Map (27 Endpoints)

### Customer-Facing
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/instant-quote?brand=construction\|broom` | 3-step quote wizard |
| GET | `/schedule` | Cal.com scheduling widget |
| GET | `/deposit-instructions/{quote_id}` | Zelle/ACH payment page |
| GET | `/review` | 5-star review gate |
| GET | `/areas/{city_slug}` | City landing pages |
| GET | `/sitemap.xml` | Dynamic SEO sitemap |
| GET | `/robots.txt` | Crawler rules |

### API
| Method | Route | Description |
|--------|-------|-------------|
| POST | `/api/quote/submit` | Create new quote |
| POST | `/api/quote/{quote_id}/photos` | Upload photos |
| POST | `/api/quote/{quote_id}/analyze` | Run AI vision analysis |
| GET | `/api/quote/{quote_id}/detail` | Get full quote details |
| POST | `/api/quotes/{quote_id}/accept` | Accept quote → deposit flow |
| POST | `/api/reviews/submit` | Submit review feedback |
| GET | `/api/photo/{photo_id}` | Serve uploaded photo |
| POST | `/webhooks/cal` | Cal.com booking webhook |

### Admin
| Method | Route | Description |
|--------|-------|-------------|
| GET | `/admin/quotes` | Quote management dashboard |
| GET | `/admin/pricing-rules` | Pricing rules editor |
| POST | `/admin/pricing-rules` | Update pricing rule |
| POST | `/admin/quotes/{quote_id}/approve` | Approve quote |
| GET | `/admin/marketing` | Social media post generator |
| GET | `/admin/marketing/completed-photos` | Get completed job photos |
| POST | `/admin/marketing/post` | Create/publish marketing post |

---

## Pricing Zones

### Core Zone ($0 travel fee)
Chesapeake, Virginia Beach, Norfolk, Portsmouth, Suffolk, Newport News, Hampton, Williamsburg, Yorktown  
**77 ZIP codes**

### Extended Zone (+$75 fee, +45min buffer)
Currituck County, Elizabeth City, Corolla / Outer Banks, NC  
**23 ZIP codes**

---

## Production Deployment

### Server: `159.203.175.39` (bizstack-prod)

```bash
# 1. Copy environment file
scp .env.example root@159.203.175.39:/opt/bizstack/quote-engine.env
ssh root@159.203.175.39
nano /opt/bizstack/quote-engine.env  # Fill in all values

# 2. Rsync code
rsync -avz --exclude='.env*' --exclude='*.db' --exclude='uploads/' \
  ./quote-engine-files/ root@159.203.175.39:/opt/bizstack/quote-engine/

# Files to sync:
#   - main.py, models.py, schemas.py
#   - services/
#   - templates/
#   - requirements-quote-engine.txt
#   - Dockerfile.quote-engine
#   - docker-compose.quote-engine.yml

# 3. Build and start
cd /opt/bizstack/quote-engine
docker compose -f docker-compose.quote-engine.yml up -d --build

# 4. Verify
curl http://localhost:8005/health
# {"status":"ok","timestamp":"..."}
```

### Nginx Configuration

Add these location blocks to your existing nginx config to proxy the quote engine:

```nginx
# Quote Engine proxy (FastAPI on port 8005)
location /instant-quote      { proxy_pass http://127.0.0.1:8005; }
location /schedule            { proxy_pass http://127.0.0.1:8005; }
location /deposit-instructions { proxy_pass http://127.0.0.1:8005; }
location /review              { proxy_pass http://127.0.0.1:8005; }
location /areas/              { proxy_pass http://127.0.0.1:8005; }
location /api/quote           { proxy_pass http://127.0.0.1:8005; }
location /api/quotes          { proxy_pass http://127.0.0.1:8005; }
location /api/photo           { proxy_pass http://127.0.0.1:8005; }
location /api/reviews         { proxy_pass http://127.0.0.1:8005; }
location /webhooks/cal        { proxy_pass http://127.0.0.1:8005; }
location /admin/quotes        { proxy_pass http://127.0.0.1:8005; }
location /admin/pricing       { proxy_pass http://127.0.0.1:8005; }
location /admin/marketing     { proxy_pass http://127.0.0.1:8005; }
location /sitemap.xml         { proxy_pass http://127.0.0.1:8005; }
location /robots.txt          { proxy_pass http://127.0.0.1:8005; }
```

Then: `nginx -t && systemctl reload nginx`

---

## Environment Variables

See [`.env.example`](.env.example) for full documentation. Key variables:

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | PostgreSQL connection string |
| `GEMINI_API_KEY` | Google Gemini vision API |
| `OPENAI_API_KEY` | OpenAI fallback vision API |
| `CALCOM_CONSTRUCTION_EVENT_SLUG` | Construction Cal.com event |
| `CALCOM_BROOM_EVENT_SLUG` | Broom Service Cal.com event |
| `ZELLE_HANDLE` | Zelle payment recipient |
| `ACH_ROUTING` / `ACH_ACCOUNT` | Bank transfer details |
| `SMTP_*` | Email configuration |
| `GOOGLE_REVIEW_URL` | Google review link |
| `FACEBOOK_REVIEW_URL` | Facebook review link |
| `YELP_REVIEW_URL` | Yelp review link |

---

## Tech Stack

- **Backend:** Python 3.11+, FastAPI, SQLAlchemy, PostgreSQL/SQLite
- **Frontend:** Jinja2, Tailwind CSS, HTMX, Uppy.js
- **Vision AI:** Google Gemini + OpenAI (dual-provider with fallback)
- **Scheduling:** Cal.com (inline embed + webhook sync)
- **Payment:** Zelle & ACH/Bank Transfer only — **zero Stripe**
- **Email:** Python smtplib via configurable SMTP
- **SEO:** JSON-LD structured data, programmatic city pages, dynamic sitemap

---

## Service Area

**701 Dana Dr, Chesapeake, VA 23321**  
Serving Williamsburg–Hampton Roads, VA & Currituck County, Elizabeth City, & Outer Banks, NC

| City | Zone | Phone |
|------|------|-------|
| Chesapeake, VA | Core | (757) 908-7121 |
| Virginia Beach, VA | Core | (757) 908-7121 |
| Norfolk, VA | Core | (757) 908-7121 |
| Williamsburg, VA | Core | (757) 908-7121 |
| Hampton / Newport News, VA | Core | (757) 908-7121 |
| Outer Banks / Currituck, NC | Extended | (757) 908-7121 |

---

## License

Proprietary — BizStack Perks LLC
