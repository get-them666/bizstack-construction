"""Social posting from the owner dashboard.

Facebook is the only platform here with a write API, so it is the only one we can
post to. Yelp and Nextdoor have no public posting API at all — their business
listings are UI-only — so this module holds copy-paste packs for those instead of
pretending an integration exists.

Everything is driven by real project photos already on the site
(static/listing/projects2). No invented jobs, prices or cities: a fabricated
before/after is the fastest way to lose a local reputation.
"""

from __future__ import annotations

# slug -> (title, scope, caption). The caption is what gets posted; keep the
# claim limited to what the photo actually shows.
PROJECTS = [
    ("full-home-remodel", "Whole-home remodel",
     "Down to the studs and back: layout, mechanicals, finishes.",
     "A whole house taken back to the studs and rebuilt. New layout, new "
     "mechanicals, new finishes throughout \u2014 one crew, one scope, one fixed "
     "price. If you are looking at a property that needs everything, this is the "
     "conversation to have before you buy it.\n\nFree on-site estimate: "
     "(757) 846-9275"),
    ("kitchen-remodel", "Kitchen remodel",
     "Cabinets, counters, tile, lighting, plumbing and electrical.",
     "A kitchen that came down to the framing and went back together as one job: "
     "cabinets, counters, backsplash, lighting, plumbing and electrical. The "
     "permits were pulled and inspected before anything was closed up.\n\n"
     "Free on-site estimate: (757) 846-9275"),
    ("bathroom-remodel", "Bathroom remodel",
     "Waterproofing, tile, fixtures, venting.",
     "Bathrooms fail at the waterproofing, not the tile. This one was stripped, "
     "re-panelled, vented properly, then tiled \u2014 so the finish you see is "
     "sitting on a substrate that will still be there in fifteen years.\n\n"
     "Free on-site estimate: (757) 846-9275"),
    ("home-addition", "Home addition",
     "Footprint, framing, roof tie-in, siding.",
     "An addition is two jobs: the new space, and tying it into the existing "
     "roof, siding and mechanicals so it does not read as bolted on. Footprint, "
     "framing, roof tie-in and finish were all one scope here.\n\n"
     "Free on-site estimate: (757) 846-9275"),
    ("deck-build", "Deck build",
     "Framing, decking, rails, stairs.",
     "New deck, built to the current span and guard requirements rather than the "
     "ones that were legal when the house went up. Framing, decking, rails and "
     "stairs.\n\nFree on-site estimate: (757) 846-9275"),
    ("new-roof", "Roof replacement",
     "Tear-off, deck repair, underlayment, shingles.",
     "A roof is the tear-off, the deck repair underneath, and the underlayment "
     "\u2014 not just the shingles you can see from the street. That is where the "
     "cheap quotes come from, and where the leaks start.\n\n"
     "Free on-site estimate: (757) 846-9275"),
    ("siding-replacement", "Siding replacement",
     "Sheathing repair, weather barrier, siding, trim.",
     "Siding replacement is really a weather-barrier job. Sheathing repaired, "
     "house wrapped, siding and trim set so water sheds instead of sitting behind "
     "it.\n\nFree on-site estimate: (757) 846-9275"),
    ("garage-to-airbnb", "Garage conversion",
     "Garage to a rentable studio: framing, insulation, mechanicals, finishes.",
     "A garage turned into rentable space: framed, insulated, heated and cooled, "
     "with its own entrance and finishes. The permit and inspection record is "
     "what makes it legal to rent \u2014 skip that and it is a liability, not an "
     "asset.\n\nFree on-site estimate: (757) 846-9275"),
    ("den-renovation", "Den renovation",
     "Layout, drywall, trim, paint, flooring.",
     "A den that was dark and chopped up, opened and rebuilt: layout, drywall, "
     "trim, paint and flooring.\n\nFree on-site estimate: (757) 846-9275"),
    ("basement-finish", "Basement finish",
     "Insulation, framing, moisture control, finishes.",
     "A finished basement lives or dies on moisture control. Insulation and "
     "framing were detailed for it before any drywall went up.\n\n"
     "Free on-site estimate: (757) 846-9275"),
    ("front-porch", "Front porch",
     "Repair and rebuild of the entry structure.",
     "A front porch rebuilt properly \u2014 structure repaired first, then the "
     "finish work. Porches rot from the bottom up, so the pretty part is the last "
     "thing you do, not the first.\n\nFree on-site estimate: (757) 846-9275"),
    ("landscaping-outdoor-living", "Outdoor living",
     "Hardscape, plantings and outdoor structure.",
     "Outdoor living space built as part of the same job: hardscape, structure "
     "and plantings, graded so water moves away from the house instead of "
     "toward it.\n\nFree on-site estimate: (757) 846-9275"),
]

# Yelp and Nextdoor have no posting API. This is the copy pack the owner pastes
# in, written for a neighbour reading it, not for a search engine.
NEXTDOOR_POST = """Hi neighbours \u2014 Shaun O'Leary here, BuildStack Construction LLC.

I'm a licensed, bonded and insured general contractor based in Chesapeake, working
all over Hampton Roads and up into Currituck and Elizabeth City. Thirty-two years
in the trade, and six of those were spent as a federal construction inspector \u2014
so I approve work the way an inspector does, because I used to be one.

What that means for you: one crew, one written scope, one fixed price. No
open-ended billing and no chasing three different trades to find out who is
supposed to show up.

Whole-home renovations, additions, kitchens and baths, roofing, siding, drywall,
decks and fences, and rental make-readies for anyone with short-term rentals.

If you've got something you've been putting off \u2014 or you just want a straight
answer about whether a repair is serious \u2014 call or text me at (757) 846-9275.
Estimates are free and on site, and I'll tell you honestly if it's a weekend job
rather than a project.
"""

YELP_BIZ_NOTE = """Yelp has no posting API. Everything below is done in the Yelp
for Business dashboard, which needs the listing claimed first.

1. Claim the listing: yelp.com/biz/claim \u2014 search the business name, pick the
   listing for BuildStack Construction LLC, and verify by phone or email. If the
   listing is already claimed by a dead login, use "Claim this business" and
   Yelp support transfers it after verification.
2. Recover access: yelp.com/for_business \u2192 Forgot password, using the email on
   the listing. If nobody knows that email, Yelp support can re-verify by phone
   call to the public number on the listing.
3. Fill the profile completely: 10+ real photos (use static/listing/projects2),
   hours, service area (Chesapeake, Virginia Beach, Norfolk, Portsmouth, Suffolk,
   Hampton, Newport News, Williamsburg, Elizabeth City), and a 750-character
   description.
4. Turn on "Request a Quote" if the category offers it, then answer every
   request within an hour during the day \u2014 response speed is the single largest
   ranking factor you control.
5. Ask for reviews only from completed jobs, and never offer anything in
   exchange. Yelp filters anything that looks solicited.
"""


# --- free channels ----------------------------------------------------------
# Everything here is free to claim or free to post. Paid lead marketplaces
# (Angi, Thumbtack, HomeAdvisor) are deliberately absent: they sell the same
# homeowner to four contractors.
CHANNELS = [
    {
        "key": "google",
        "name": "Google Business Profile",
        "why": "The one that matters most. Most local contractor calls start here.",
        "url": "https://business.google.com/",
        "steps": [
            "Claim or recover the listing at business.google.com using the Google "
            "account that should own it.",
            "Primary category: General Contractor. Add secondaries: Remodeler, "
            "Roofing contractor, Deck builder, Bathroom remodeler.",
            "Service area: Chesapeake, Virginia Beach, Norfolk, Portsmouth, "
            "Suffolk, Hampton, Newport News, Williamsburg, Elizabeth City.",
            "Upload 10+ real photos from static/listing/projects2 and one logo.",
            "Turn on messaging, then answer within the hour during the day.",
            "Ask every completed job for a review \u2014 nothing else compounds like this.",
        ],
    },
    {
        "key": "facebook",
        "name": "Facebook Page",
        "why": "Before/after photos get shared in local groups, which is free reach.",
        "url": "https://www.facebook.com/BuildStackConstructionLLC/",
        "steps": [
            "Connect the Page below with a Page access token to post from here.",
            "Post 2\u20133 real project photos a week; one photo, one honest sentence.",
            "Share each post into the local town/neighbourhood groups you belong to.",
            "Reply to every comment and message the same day.",
        ],
    },
    {
        "key": "nextdoor",
        "name": "Nextdoor Business",
        "why": "Neighbours recommend contractors by name here more than anywhere else.",
        "url": "https://nextdoor.com/business/",
        "steps": [
            "Claim the free business page at nextdoor.com/business.",
            "Set the service area to the cities you actually work in.",
            "Post the neighbour copy below once a month \u2014 not weekly.",
            "Ask happy customers to recommend you in their neighbourhood feed; a "
            "recommendation from a real neighbour outperforms any ad.",
        ],
    },
    {
        "key": "yelp",
        "name": "Yelp Business",
        "why": "Free listing, and it ranks for '<city> contractor' searches.",
        "url": "https://biz.yelp.com/",
        "steps": [
            "Claim or recover the listing at biz.yelp.com.",
            "Complete the profile, add 10+ real photos, set hours and service area.",
            "Answer every quote request within the hour.",
            "Never ask for reviews in exchange for anything.",
        ],
    },
    {
        "key": "houzz",
        "name": "Houzz",
        "why": "Free profile; project photos keep working for you for years.",
        "url": "https://www.houzz.com/for-pros",
        "steps": [
            "Create a free pro profile.",
            "Upload the before/after pairs from static/listing/projects2 as projects.",
            "Link back to construction.bizstackperks.com/portfolio.",
        ],
    },
    {
        "key": "craigslist",
        "name": "Craigslist \u2014 Hampton Roads",
        "why": "Still the cheapest place to catch a homeowner mid-repair in this market.",
        "url": "https://norfolk.craigslist.org/",
        "steps": [
            "Post under services \u2192 skilled trade services, once every 48 hours.",
            "Use the short ad copy below, real photos, and the business number.",
            "Do not repost more often than every 48 hours or it gets ghosted.",
        ],
    },
    {
        "key": "bbb",
        "name": "BBB listing",
        "why": "Free listing; some homeowners and property managers check it before calling.",
        "url": "https://www.bbb.org/",
        "steps": [
            "Create the free listing (accreditation is optional and paid \u2014 skip it).",
            "Match the name, address and phone exactly to the website footer.",
        ],
    },
]

GOOGLE_PROFILE_DESCRIPTION = """BuildStack Construction LLC is a licensed, bonded and
insured general contractor serving Hampton Roads, Virginia and Currituck County,
North Carolina. Founded by Shaun O'Leary after 32 years in the trade \u2014 including
six as a federal construction inspector \u2014 we handle whole-home renovations,
additions, kitchens, bathrooms, roofing, siding, drywall, plumbing, electrical,
tile, flooring, decks and fences with one crew, one written scope and one fixed
price. We also prepare and manage short-term rental properties from renovation
through listing and turnover. Free on-site estimates: (757) 846-9275."""

CRAIGSLIST_AD = """Licensed, bonded & insured general contractor \u2014 Hampton Roads

32 years in the trade, six of them as a federal construction inspector. One crew,
one written scope, one fixed price \u2014 no open-ended billing.

\u2022 Whole-home renovations and additions
\u2022 Kitchens and bathrooms
\u2022 Roofing, siding, drywall
\u2022 Decks, fences, carpentry
\u2022 Rental make-readies and STR renovations

Free on-site estimate, and a straight answer about whether a repair is serious.
Call or text (757) 846-9275.
construction.bizstackperks.com"""


def ensure_schema(conn) -> None:
    """Track what has actually been posted, so nothing goes out twice."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS social_posts ("
            " id SERIAL PRIMARY KEY,"
            " platform VARCHAR(20) NOT NULL DEFAULT 'facebook',"
            " slug VARCHAR(80) NOT NULL,"
            " post_id VARCHAR(200),"
            " caption TEXT,"
            " posted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),"
            " UNIQUE (platform, slug));"
        )
    conn.commit()


def posted_slugs(conn, platform: str = "facebook") -> set:
    with conn.cursor() as cur:
        cur.execute("SELECT slug FROM social_posts WHERE platform = %s;", (platform,))
        return {r["slug"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}


def record_post(conn, slug: str, post_id: str, caption: str,
                platform: str = "facebook") -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO social_posts (platform, slug, post_id, caption) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (platform, slug) DO UPDATE SET post_id = EXCLUDED.post_id,"
            " caption = EXCLUDED.caption, posted_at = NOW();",
            (platform, slug, post_id, caption),
        )
    conn.commit()
