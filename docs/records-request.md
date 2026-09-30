# Getting applicant names + contact details for Hampton Roads permits

**Status:** draft request letters, ready to send. Nothing here has been sent —
these need your signature and your real contact details.

## Why this is worth doing

The open permit feeds we now ingest (Virginia Beach ArcGIS, Norfolk Socrata)
give address, scope and date — but **no applicant name, phone or email**:

- Virginia Beach publishes the applicant only as `CreatedBy`, an opaque portal
  handle like `PUBLICUSER55521`.
- Norfolk's dataset has no applicant or owner field at all.

Those records are still held by the city. A VFOIA request is the sanctioned way
to get them, and under Va. Code § 2.2-3704 the city must respond within **five
working days** with the records, a fee estimate, or a written reason.

What we are *not* doing: scraping the VB property-tax search. It returns owner
names but answers programmatic requests with `403 "Direct API access is not
permitted."` Automating it would be working around an explicit refusal, so the
records request is the correct route instead.

## What to ask for

Ask for the fields that identify the **applicant** on residential permits. Do
not ask for anything broader than needed — narrower requests get answered
faster and cost less, and a request for "all permit data" invites a large fee
estimate and more chances for a partial denial.

Per permit, for a defined date window:

- Permit number
- Property address and parcel/GPIN
- Permit type and work description
- Issue date
- **Applicant name**
- **Applicant phone number**
- **Applicant email address**
- Contractor name and license number (to filter out jobs already engaged)

Ask for a **rolling window** rather than all history. Once you have the first
response, ask for the newest N days each month — those repeat requests are
routine and far cheaper than one for the full archive.

## Where to send

### City of Virginia Beach

Permit records are held by Permits & Inspections, which also runs a FOIA form:

- Permits & Inspections — `perminsp@vbgov.com`, 757-385-4211
  Municipal Center, Building 3, 2403 Courthouse Drive, Virginia Beach, VA 23456
- City FOIA Request Center — `foia@vbgov.gov` (citywide specialist)
- Port: Virginia Beach also has an Accela portal
  (`aca-prod.accela.com/CVB`) with a "Permit Records" search, which is useful for
  spot-checking an address by hand.

### City of Norfolk

Norfolk routes Planning records through its FOIA team:

- `foia@norfolk.gov`
- Portal: `https://norfolk.justfoia.com/publicportal/home/newrequest`
  (choose the **general City records** option, not public safety)
- Mail: FOIA Request, Norfolk City Hall, 810 Union Street, Suite 409,
  Norfolk, VA 23510

Norfolk also publishes open data questions to `OpenData@norfolk.gov` — worth a
separate, narrower email asking whether an applicant-contact field exists in a
feed they license but do not publish.

### City of Chesapeake

Chesapeake runs its permits through eBUILD, on the same Accela platform as
Virginia Beach, so its record layout and FOIA handling are similar. This matters
because Chesapeake's open ArcGIS "Development Tracking" layer is **not usable as
a lead feed**: it is land-use actions (use permits, subdivisions, rezoning) with
a newest entry from April 2022, and no applicant fields. eBUILD is where the
live building permits live.

- Permits & Inspections — `permitsupport@CityOfChesapeake.net`, 757-382-2489
- City of Chesapeake, 306 Cedar Road, Chesapeake, VA 23322
- General FOIA — `foia@CityOfChesapeake.net`

### Optional, if you expand past these three

- City of Suffolk — Suffolk Public Works / Planning
- City of Hampton — Development Services Center (757-728-2444)
- City of Newport News — Development Services

Send one request per city. No city will answer for another, and Chesapeake will
not answer for Virginia Beach even though both use Accela.

## Costs and timing

§ 2.2-3704(F) lets the city charge **actual cost** for search and duplication,
and requires them to tell you in advance if they will charge. Ask for the cost
estimate up front. Keep the response window tight — a week of permits is far
cheaper to produce than a year — and ask for **electronic delivery** (CSV or
spreadsheet) rather than PDFs, which cost more to produce and parse.

If five working days pass with no response, that is a violation and the next
step is a complaint to the Virginia FOIA Council (foiacouncil@dls.virginia.gov)
or to the circuit court. In practice a polite follow-up email usually resolves
it first.

## Once records arrive

1. Match to `job_leads` on permit number (that is the dedupe key
   `ingest_permits()` already uses).
2. Store applicant name / phone / email on the `leads` row the permit promotes
   to, so it lands on `/leads?lane=contactable` alongside your other leads.
3. Respect the contact caps already in `auto_reply.py` — `EMAIL_DAILY_CAP` and
   `TEXT_DAILY_CAP` exist precisely because this data will be easy to abuse.
4. These are residential property owners who have not hired anyone yet. That is
   a business relationship, not a marketing list. Keep volume sane, honour
   opt-outs, and stay inside CAN-SPAM and the Do Not Call provisions.

## Draft letter

Fill in the bracketed fields, sign, and send.

---

**VIA EMAIL: perminsp@vbgov.gov**
**CC: foia@vbgov.gov**

[Date]

City of Virginia Beach — Permits & Inspections Division
Municipal Center, Building 3
2403 Courthouse Drive
Virginia Beach, VA 23456

**Re: Request for public records under the Virginia Freedom of Information Act
(Va. Code § 2.2-3700 et seq.) — building permit applicant records**

Dear [name or "Records Custodian"]:

Under the Virginia Freedom of Information Act, I request the following public
records from the City of Virginia Beach Permits & Inspections Division:

For all **building permits issued between [start date] and [end date]** for
residential work, in electronic form (CSV or spreadsheet preferred):

1. Permit number
2. Property address and parcel identification number (GPIN)
3. Permit type and work/scope description
4. Permit issue date
5. Applicant name
6. Applicant telephone number
7. Applicant email address
8. Contractor name and contractor license number, if different from the applicant

I make this request in my capacity as a licensed general contractor operating in
the City of Virginia Beach. My purpose is to offer construction services to
property owners who have initiated residential improvement projects. I will not
resell or redistribute the records, and I will limit any contact to a single
solicitation regarding the specific project.

I understand the City may charge reasonable fees not to exceed its actual cost
of searching and duplicating records pursuant to § 2.2-3704(F). **Please send me
a cost estimate before producing the records**, and please deliver them
electronically to this address. If any portion of this request is denied in
whole or in part, please cite the specific exemption and provide the name and
address of the person who made the determination, as required by
§ 2.2-3704(B).

I am happy to narrow the date range or the permit types if that would reduce the
cost of processing.

Sincerely,

[Your full legal name]
[Company name]
[Virginia contractor license number]
[Address]
[Phone]
[Email]

---

**Send the equivalent to Norfolk**, addressed to foia@norfolk.gov or filed
through https://norfolk.justfoia.com/publicportal/home/newrequest (general City
records), substituting "City of Norfolk" and Norfolk's Planning and Permits &
Inspections Division, 757-664-6565 / planning@norfolk.gov.