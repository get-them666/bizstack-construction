# Kit (ConvertKit) setup — BuildStack Construction

Kit is email marketing: **broadcasts** (one-off emails), **sequences** (automated
follow-ups), **forms/landing pages**, and subscriber tags. It does **not** run
ads. "Free ads" means Google Business Profile posts, Facebook/Instagram posts,
and local groups — Kit is the list those posts send people to.

Brand assets already in the repo:

- Logo: `static/brand/construction-email-logo.png` (720×180)
- Email template: `static/brand/kit-email-template.html`
- Palette: `#0a0a0a` (header/footer), `#48413d` (accent rule),
  `#059669` (estimate CTA), `#4f46e5` (phone links), Georgia headings,
  Arial body/footer.

## 1. Account settings (once)

1. **Account → Settings → General**: business name `BuildStack Construction LLC`,
   website `https://construction.bizstackperks.com`, mailing address
   `701 Dana Dr, Chesapeake, VA 23321`, timezone `America/New_York`.
2. **Email → Sending address**: `hello@bizstackperks.com`.
3. **Domain authentication**: add the SPF/DKIM records Kit gives you at the DNS
   host. Do not skip this; without it, broadcasts land in spam.
4. **Brand → Logo**: upload the PNG above. **Brand → Colors**: the five hex codes.
5. **API**: Settings → Advanced → API key. Paste it into `KIT_API_KEY` on the
   Droplet; the importer below uses it.

## 2. Forms

| Form | Purpose | Tag applied |
|---|---|---|
| Free estimate | Main lead magnet, embed on the site | `estimate-request` |
| Newsletter | Footer signup, monthly tips | `newsletter` |
| Storm / seasonal | Emergency repair landing page | `storm-damage` |

## 3. Sequences (automated)

**A. New estimate request** (3 emails, days 0/2/5)
1. "Your estimate request — next step" — confirm we got it, ask for photos and
   the address, offer a walkthrough time.
2. "What changes the price" — size, access, materials, concealed conditions;
   no invented numbers.
3. "Still want that estimate?" — one-question close, phone CTA.

**B. Bid follow-up (commercial GCs)** (2 emails, days 0/4)
1. "Following up on [project] bid" — confirm receipt, ask who is reviewing and
   the decision date, offer scope questions.
2. "Anything I can clarify?" — short, direct.

**C. Past-customer reactivation** (2 emails, days 0/10)
1. "How's the [project] holding up?" — check-in, no pitch.
2. "Planning the next project?" — seasonal maintenance offer.

**D. Number correction** (one broadcast, not a sequence)
Subject: `BuildStack's number changed — (757) 846-9275`
Body: use the template, one paragraph: old `757-908-7121` is retired, use
`(757) 846-9275` for calls/texts; reply for a free estimate.

## 4. Broadcasts (manual)

- **Monthly newsletter**: one finished job (photo + scope + city), one seasonal
  maintenance tip, one CTA to the estimate page. Never invent prices or dates.
- **Seasonal**: storm prep (roof/drainage), spring exterior, fall gutter/winterization.
- **Number correction**: send once to the `dnc-correction` tag after import.

## 5. Import the 282 past recipients

```bash
cd /opt/bizstack && docker compose exec -T -e PYTHONPATH=/app construction \
  python scripts/kit_import.py --apply --tag dnc-correction --limit 300
```

Dry run without `--apply`. Idempotent: existing subscribers are skipped.

## 6. Free "ads" (not Kit)

- Google Business Profile: weekly post, same photo + scope from the newsletter.
- Facebook page: reuse the newsletter content; do not boost with money.
- Local groups/referral partners: one-to-one, no scraped lists.
- Website: the estimate form feeds Kit automatically once the API key is set.

## What still needs a human

Account creation, sender verification, and the DNS records are the three steps
Kit will not let an API key do. Everything after that is scripted.
