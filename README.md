# Zaheendata

National, **download-first** warehouse for qualified local-service leads across 17 NAICS sub-sectors.

Free public files are loaded into DuckDB *before* scoring. Live site crawls (and later Google Places) only run on businesses that already have an owned website. The earlier Austin plumbing export failed because it started from Texas sales-tax permits and SBA PPP rows — names, sometimes phones, almost never a website.

```
Overture Places (S3 GeoParquet)
        │  bbox + taxonomy.primary
        ▼
   DuckDB warehouse          ◄── license CSVs, optional PPP overlay
        │
        ├─ filter (franchise / directory / no website)
        ├─ Common Crawl snapshots (optional) + live HTML
        ├─ tech fingerprints (Shopify, QuickBooks, Jobber, …)
        ├─ score 0–2 × 5 axes
        └─ export top N
```

Google Places is an optional later overlay (`zaheendata run`) for review counts. It is not required to build the national file.

## Free sources (download these first)

| Layer | Where | What you actually get |
| --- | --- | --- |
| **Discovery** | [Overture Maps Places](https://docs.overturemaps.org/guides/places/) on public S3 `s3://overturemaps-us-west-2/release/<date>/theme=places/type=place/` | Name, **website**, **phone**, email, address, lat/lng, brand, socials, taxonomy. Slice with a bbox (US / state / metro) and `taxonomy.primary` |
| **Tech stack** | Direct GET of home/contact/about, optional [Common Crawl](https://commoncrawl.org/) WARC | Shopify, QuickBooks, ServiceTitan, Jobber, Square, Mailchimp, WordPress, … from script URLs and copy |
| **Person owner** | State license bulk files (TX TDLR SODA works unattended; CA/FL/NY are catalogued as manual downloads) | License number + person name. LLC names are **not** owners |
| **Size proxy** | SBA PPP FOIA (optional overlay, not discovery) | `jobs_reported` on a name match. Stale, incomplete |
| **Universe counts** | Census CBP | County establishment counts by 5–9 / 10–19 employees. Counts only, not a firm list |
| **Reviews / “busy”** | Google Places API (New) later | Rating, review count, 5 recent reviews. Needed for `busy_and_growing` |

There is **no free national firm-level 5–19 employee list**. Review volume plus franchise drops is the public proxy. Overlay Data Axle (often via a library card) or a small Apollo pull if you must enforce headcount.

Directory hosts (HomeAdvisor, Angi, Yelp, Facebook, `business.site`, …) are not the business’s own website and are dropped before crawl.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

No API key is required for `download` / `build` on Overture. DuckDB talks to the public S3 bucket with `httpfs`.

## Commands

```bash
# Confirm public S3 + DuckDB
zaheendata doctor --geo Austin,TX

# National-capable build: download Overture → warehouse → crawl tools → score
zaheendata build --geo Austin,TX --subsector plumbing_hvac --limit 200 --out data/austin-plumbing.csv
zaheendata build --geo TX --subsector plumbing_hvac --out data/tx-plumbing.csv
zaheendata build --geo US --subsector plumbing_hvac --limit 5000 --out data/us-plumbing.csv

# Stepwise (download once, reuse forever)
zaheendata download places --geo US --subsector plumbing_hvac --out data/raw/overture_plumbing_hvac_US.parquet
zaheendata ingest --places data/raw/overture_plumbing_hvac_US.parquet
zaheendata enrich --subsector plumbing_hvac --mode live
zaheendata score --subsector plumbing_hvac --keep 100 --out data/leads.csv
zaheendata stats

# License catalog (bulk SODA vs manual board extracts)
zaheendata licenses --subsector plumbing_hvac
zaheendata build --geo Austin,TX --subsector plumbing_hvac --licenses --limit 200

# Optional Google Places metro overlay (paid/quota later)
zaheendata run --subsector plumbing_hvac --city Austin --state TX --out data/austin-google.csv

zaheendata subsectors
zaheendata diagnose path/to/old-export.csv
```

`--geo` is `US`, a state code (`TX`), or `City,ST` (`Austin,TX`, `Chicago,IL`, `Los Angeles,CA`, …). Start with a metro or one state; a full-US plumbing extract is the same program with a larger bbox.

## Warehouse

DuckDB file `data/warehouse.duckdb` (override with `ZAHEENDATA_WAREHOUSE`):

- `places` — Overture businesses, including those without a website
- `licenses` — person owners where a board file exists
- `ppp` — optional jobs_reported overlay
- `snapshots` / `technologies` — HTML fetches and detected tools
- `leads` — scored export rows

Tech fingerprints live in `src/zaheendata/fingerprints.py` (QuickBooks, Xero, Shopify, WooCommerce, ServiceTitan, Jobber, Housecall Pro, Square, Stripe, Mailchimp, HubSpot, GoHighLevel, …). Crawl HTML is matched against script CDNs and product copy; results land in `tech_stack` / `tool_categories`.

## Scoring

Same 0–2 axes as the sub-sector guide: `busy_and_growing`, `admin_pain`, `multi_tool`, `owner_reachable`, `subsector_signal`. On the free path `busy_and_growing` stays 0 until Google review counts are joined. `owner_reachable` requires a **person** name, not an LLC.

## Why the uploaded Austin file was not a lead list

Diagnose on `austin-plumbing-200.csv`: 213 rows, **1 website**, **0 crawled**, **0 emails**, **0 place ids**, **0 licenses**. Sources were tax permits and PPP. Owner was the LLC name on 144 rows. That list is a registry dump with a sort key.

Outreach still has to follow CAN-SPAM and TCPA. This repo builds a research file; it does not send mail or texts.
