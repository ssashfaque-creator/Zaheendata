# Zaheendata

An enterprise business-directory extraction engine. It builds scored, owner-enriched shortlists
of small businesses (5–19 employees) for 17 NAICS sub-sectors from **free government and public
data** – SBA PPP loan records, SAM.gov, state and city business registries, licence boards, Google
Maps and trade directories – then applies the four-step method from the sub-sector guide:

1. **Build a long list (300–500)** from government registries filtered by NAICS, employee band and
   geography, cross-checked against the sub-sector's own source (licence lists, Houzz, Thumbtack,
   Vagaro, ISSA, ASTRA, ...).
2. **Remove poor fits** – franchises and chains, no website, fewer than 10 Google reviews,
   closed, outside the target area.
3. **Score on fit signals (0/1/2 each)** – busy and growing, visible admin pain, several
   separate tools, owner-run and reachable, plus the sub-sector specific signal.
4. **Keep the top 100** and add owner name, email and phone.

Every stage is persisted, resumable, rate-limited, cached, logged and costed.

```
sources ──▶ collect ──▶ resolve ──▶ filter ──▶ enrich ──▶ score ──▶ contacts ──▶ export
 registries  raw rows    entity      poor-fit   maps match 5 signals  owner        CSV/XLSX/
 licences    per source  resolution  removal    website    0–10       email/phone  JSON/API
 maps                    (dedupe)               reviews
 directories                                    tech stack
```

## Quick start

```bash
make install                     # uv venv + editable install with dev extras
cp .env.example .env             # add the API keys you have (all optional)
.venv/bin/zaheen demo            # full offline run on bundled fixtures, writes data/demo-shortlist.csv
.venv/bin/zaheen subsectors      # the 17 sub-sectors, NAICS codes and best-fit signals
.venv/bin/zaheen sources         # which connectors are configured
.venv/bin/zaheen datasets check  # probe every government dataset in config/public_datasets.yaml
```

Run a real campaign:

```bash
zaheen db-init
zaheen datasets ppp-download                                            # one-off: SBA PPP FOIA CSVs
zaheen plan plumbing_hvac --state TX --level county                     # Census counts → scope advice
zaheen campaign create plumbing_hvac --location "Austin, TX"            # metro scope
zaheen campaign create pest_control --state TX                          # state-wide scope
zaheen campaign create hobby_toy_game_stores                            # national scope
zaheen campaign run <campaign-id> --import exports/my_registry_pull.csv # merge any CSV/XLSX export
zaheen campaign export <campaign-id> exports/plumbing-austin.xlsx
```

Or over HTTP (`zaheen serve`, OpenAPI docs at `/docs`):

```bash
curl -X POST localhost:8000/campaigns -H 'content-type: application/json' \
  -d '{"name":"Plumbing Austin","subsector_key":"plumbing_hvac","geography":{"locations":["Austin, TX"]}}'
curl -X POST localhost:8000/campaigns/<id>/run
curl localhost:8000/campaigns/<id>                       # stage, stats, counts, cost ledger
curl "localhost:8000/campaigns/<id>/export?fmt=xlsx" -o shortlist.xlsx
```

Production: `docker compose up --build` starts Postgres, the API and two workers. Queue campaigns
with `POST /campaigns/{id}/queue`; workers claim them with `SELECT ... FOR UPDATE SKIP LOCKED`, so
you scale by adding worker replicas.

## What you get per business

Identity (name, phone E.164, website, domain, address, geo, Google place id, NAICS, employee
count, licence number), engagement (rating, review count, reviews in the last 90 days, last
review date), website intelligence (online booking and provider, quote/contact forms, "call for a
quote", emergency service, maintenance plans, recurring service, free estimates, financing, gift
cards, walk-ins, retail products, online store and platform, events, Instagram selling, crews/staff
hints, chair counts, franchise vs locally-owned language), detected tool stack (Square, Shopify,
Mailchimp, Vagaro, Jobber, ServiceTitan, HubSpot, Podium, ...), admin-pain mentions from reviews
with examples, owner contact (name, title, email, phone, LinkedIn, source, confidence), the five
signal scores with written evidence, rank, provenance of every source that contributed, and
compliance flags (role-account email, suppression hits).

## Data sources

| Role | Connector | Needs |
|---|---|---|
| Long list (NAICS + 5–19 employees) | **SBA PPP loan data** (NAICS, `JobsReported`, franchise name, owner), **SAM.gov** entity search, **state/city business registries** on Socrata (TX, CA, WA, CT, LA, ...; `config/public_datasets.yaml`), **CSV/XLSX import** of any registry pull | free / free key / none / file |
| Google Maps data | Google Places API (New) text search + Place Details (newest reviews); registry rows are matched to their Maps listing during enrichment | `ZAHEEN_GOOGLE_MAPS_API_KEY` |
| Cross-check | State licence boards (TDLR, WA L&I, OR CCB, DE DPR, NYC DCWP, Chicago BACP, ...), Yelp Fusion, OpenStreetMap Overpass, public directories (`config/directories.yaml`) | none / key / config |
| Scope planning | Census County Business Patterns establishment counts (`zaheen plan`) | `ZAHEEN_CENSUS_API_KEY` (free) |
| Enrichment | Website crawler + Wappalyzer-style fingerprints (`config/tech_fingerprints.yaml`), owner waterfall (record → SAM.gov → Secretary of State filings → website) | none |
| Experimental | Playwright Google Maps scraper (off by default; ToS caveats) | opt-in |

No paid list vendors are involved. The only keys are free: Google Maps (pay-as-you-go with a monthly
credit), api.data.gov for SAM.gov and api.census.gov for planning.

See [docs/DATA_SOURCES.md](docs/DATA_SOURCES.md) for every source, what it yields, cost, terms and
the sub-sector mapping; [docs/SCORING.md](docs/SCORING.md) for the exact scoring rules;
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for internals and scaling.

## Configuration

All settings are environment variables prefixed `ZAHEEN_` (see `.env.example`). Business rules
live in YAML so they can be tuned without code changes:

- `config/subsectors.yaml` – 17 sub-sectors: NAICS, scope, cross-check sources, search queries,
  Yelp categories, OSM tags, keyword filters, best-fit rule.
- `config/public_datasets.yaml` – government registries and licence boards (Socrata/CSV) with
  column mappings, NAICS matching mode and the role each plays (long list, cross-check, owner
  lookup). Validate with `zaheen datasets check`.
- `config/franchises.yaml` – franchise/chain brand and domain lists.
- `config/tech_fingerprints.yaml` – technology fingerprints and categories.
- `config/directories.yaml` – HTML directory scraper definitions.

## Development

```bash
make check        # ruff + mypy + pytest
make demo         # offline end-to-end run
make serve        # API with auto-reload
```

Tests run fully offline using fixture records and fixture websites (`tests/fixtures`).

## Compliance notes

The engine builds research lists and never sends outreach. Exports carry the data provenance,
role-account flags and suppression results so an outreach team can honour CAN-SPAM (email) and
TCPA (calls/texts). Load opt-outs, DNC scrubs and existing customers with `zaheen suppress` or
`POST /suppressions`. Crawling respects robots.txt, identifies itself with a contact URL and is
rate-limited per host. Read the terms notes in `docs/DATA_SOURCES.md` before enabling any scraper.
