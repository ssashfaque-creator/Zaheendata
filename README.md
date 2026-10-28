# Zaheendata

Places-first pipeline that builds a **qualified** local-service lead list from free and public sources, using the scoring rules in the attached sub-sector guide.

The previous Austin plumbing export failed because it started from Texas sales-tax permits and SBA PPP rows. Those files have names and sometimes phones. They do **not** have websites, Google reviews, or owner emails, which is why 212 of 213 rows had a blank website, none were crawled, and every score was 0 or 1.

## What changed

Do not enrich 200 tax records one by one. Discover businesses from the source that already has the fields you filter and score on.

```
Google Places Text Search  ──►  cheap filters  ──►  crawl 3–4 pages  ──►  score  ──►  top 100
        │                     (franchise, no site,     only survivors
        │                      <10 reviews)
        └── join TDLR / TSBPE licenses for owner name + license #
```

| Stage | Source | Cost / time | What you get |
| --- | --- | --- | --- |
| Discovery | Google Places API (New) Text Search | ~20–40 calls per city/trade. Free inside the 1,000-call/month Enterprise cap | Name, phone, **website**, address, rating, **review count**, place id, 5 recent reviews |
| Universe / owner | TDLR open data (`7358-krk7`) and TSBPE Responsible Master Plumber CSV | Free bulk, seconds | License number, person owner name. HVAC A/C contractor rows have **no phone/address** in the public file; electrical and salon shops do |
| Size context | Census CBP (optional key) | Free | County establishment counts by 5–9 and 10–19 employees. Counts only, not a firm list |
| Website details | Direct GET of home/contact/about/services | Free, ~2–5 min for 200 sites at 8 concurrent | Email, booking widget, quote form, emergency/maintenance copy, tech stack |
| Do not use as discovery | TX sales-tax permits, SBA PPP | Wasted time | No website, stale size, wrong NAICS, distributors mixed in |

Employee count 5–19 is **not** in any free firm-level file. Use Google review volume plus franchise/chain drops as the public proxy. Overlay Data Axle (often free via a public library card) or a small Apollo/ZoomInfo pull if you must enforce headcount.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add GOOGLE_MAPS_API_KEY
```

Enable **Places API (New)** on the Google Cloud project. Text Search requests ask for `websiteUri`, `rating`, and `userRatingCount` in one call so you do not pay for Place Details on every row.

## Commands

```bash
# Coverage post-mortem on an old export
zaheendata diagnose path/to/austin-plumbing-200.csv

# Austin plumbing + HVAC, top 100
zaheendata run --subsector plumbing_hvac --city Austin --state TX --out data/austin-plumbing.csv

# Plumbing licenses from TSBPE (download CSV from the board if Cloudflare blocks the fetch)
zaheendata run --subsector plumbing_hvac --tsbpe-file ~/Downloads/RMP.csv

zaheendata subsectors
```

`run` writes the same 60-column CSV the earlier program used, so ranking sheets keep working.

## Why the uploaded file is not a lead list

Diagnose on `austin-plumbing-200.csv`:

- 213 rows, 100 marked `selected`
- **1 website**, **0 crawled**, **0 emails**, **0 Google place ids**, **0 reviews**
- Sources: 162 tax permits, 63 PPP loans
- **0 license numbers** (the guide’s plumbing/HVAC cross-check)
- Owner field is the LLC name on 144 rows, then scored as `owner_reachable=1`
- Johnson Controls, Baker Distributing, Freedom Solar, and Mr Rooter still sit in “selected”

That list is a raw registry dump with a sort key, not qualified demand.

## Efficiency rules the code enforces

1. **Places is discovery**, not a per-row afterthought. One Text Search page returns 20 businesses that already have websites and review counts.
2. **Filter before crawl.** Franchises, no website, and under 10 reviews never hit the network.
3. **Crawl only survivors**, 8 at a time, 8s timeout, 4 pages max, HTML cached.
4. **No BuiltWith / LinkedIn scrape on the full set.** Tech stack comes from script URLs and known booking widgets. Owner names come from the license file and the About page.
5. **Do not treat an LLC name as an owner.** `owner_reachable` requires a person.

## Pilot order (from the guide)

Start with plumbing/HVAC, electrical, and beauty salons in one metro. Those trades have public license files plus dense Google listings. Retail, pest, and “other building services” need a whole state to reach 100.

Outreach still has to follow CAN-SPAM and TCPA. This repo builds a research file; it does not send mail or texts.
