# Data sources: what we need, where it comes from, how it is obtained

The brief needs five kinds of data per business. This document maps each to concrete sources,
the connector that implements it, how the data is obtained, what it costs and the terms that
apply.

| Need | Fields | Primary source | Cross-check / fallback |
|---|---|---|---|
| Long list by NAICS + 5–19 employees + area | legal name, NAICS, employee count, address, phone, website, owner name | Data Axle, D&B Hoovers, ZoomInfo, Apollo (API or export) | sub-sector source below |
| Google Maps presence and reviews | place id, rating, review count, newest reviews, business status, categories | Google Places API (New) + Place Details | Yelp Fusion, OSM |
| Website intelligence | booking/quote intake, plans, services, gift cards, store, events, owner name, emails | our crawler | – |
| Tool stack | payments, e-commerce, booking, email marketing, CRM, field-service software | fingerprints on crawled HTML | BuiltWith API |
| Owner contact | name, title, email, direct phone, LinkedIn | vendor record → Apollo people → Hunter → website | LinkedIn (manual) |

## 1. List vendors (step 1: the long list)

Vendors filter by exactly the two fields the brief relies on: NAICS code and employee band.

| Vendor | How we use it | Connector | Notes |
|---|---|---|---|
| **Data Axle** (Platform API) | `places/search` filtered by `primary_naics_code`, `location_employee_count` 5–19, state/city or geo-radius. Returns executive contacts and franchise/chain flags. | `data_axle` (`ZAHEEN_DATA_AXLE_API_KEY`) | Contract required; attribute names depend on your package (`ATTRIBUTES` in `sources/data_axle.py`). |
| **Apollo.io** | `mixed_companies/search` with `organization_num_employees_ranges=["5,19"]`, location and industry keywords; `mixed_people/search` + `people/match` for owner emails. | `apollo` (`ZAHEEN_APOLLO_API_KEY`) | No NAICS filter: keyword tags per sub-sector in `subsectors.yaml`. `people/match` consumes credits; only run for the shortlist (stage `contacts`). |
| **D&B Hoovers, ZoomInfo, Data Axle Genie exports** | Export CSV/XLSX from the vendor UI with NAICS + employee filter, pass with `--import`. Columns auto-map (`sources/csv_import.py: SYNONYMS`). | `csv_import` | The most common enterprise workflow: contracts often forbid API resale but allow exports. |

The vendor record is trusted for NAICS, employee count and owner name; Google is trusted for the
consumer-facing name, rating, reviews and status (see `resolve.py: MAPS_PREFERRED / VENDOR_PREFERRED`).

## 2. Google Maps data

### Google Places API (New) — the compliant route (`google_places`)

- **Text Search** `POST places:searchText` with `textQuery` ("plumber in Austin, TX") and a
  `locationBias` circle per search cell; 20 results per page, 3 pages, field mask limited to what
  we store (Pro SKU: id, name, address components, location, phone, website, rating, review count,
  status, types, hours). Price ≈ $32 per 1,000 requests (tracked in the cost ledger).
- **Place Details (legacy endpoint)** for `reviews` with `reviews_sort=newest`, `rating`,
  `user_ratings_total`, `business_status`. The legacy endpoint is used because the New API cannot
  sort reviews by recency, and "recent reviews" is a scoring signal. ≈ $20 per 1,000 (Atmosphere).
  Only 5 reviews are returned per place; recency is judged from those five newest.
- Responses are cached on disk (`ZAHEEN_HTTP_CACHE_TTL_SECONDS`, default 7 days) so re-running
  a stage never re-bills.
- Budget rule of thumb for one metro sub-sector: ~15 text-search pages + ~300 detail calls
  ≈ $7. A state-wide retail sub-sector (grid of ~150 cells × 3 queries) ≈ $15–40.
- **Terms:** Places data may be cached for 30 days except place ids (indefinitely). Do not display
  it on non-Google maps. Review text should be treated as transient; keep the derived signals.

### Google Maps scraper (`google_maps_scraper`, Playwright) — experimental, off by default

Scrolls the Maps result feed and parses listing cards (name, rating, review count, phone,
website, coordinates). Google's Terms of Service prohibit automated access; this connector is
provided for organisations that have made their own legal assessment, and for budget-constrained
research. Expect breakage when the DOM changes, CAPTCHA pressure and IP throttling. Enable with
`ZAHEEN_ENABLE_GOOGLE_MAPS_SCRAPER=true`, `pip install 'zaheendata[browser]'`,
`playwright install chromium`, and run it with low concurrency through a residential proxy.

### Google Business Profile owner responses

The "owner named on Google profile" signal in the brief (owner replies, "from the owner" posts)
is not exposed by the Places API. It is approximated by the vendor owner record, the website owner
extraction and Apollo. If you scrape, the owner-reply byline can be added to `owner_name_candidates`.

## 3. Sub-sector cross-check sources (step 1b: catch what the vendor missed)

| Sub-sector | Cross-check in the brief | How the engine gets it |
|---|---|---|
| Plumbing/HVAC, Electrical, Roofing, Remodelers | State contractor / electrical licence lists | `state_license`: Socrata open-data APIs (TX TDLR, NYC DCWP …) or CSV extracts (FL DBPR, CA CSLB). Configure per state in `config/license_sources.yaml` (dataset, column map, licence-type regex). Yields legal name, address, phone, licence number/status, often owner name. |
| Pest control | State pest-control licences | Same connector (TX Dept. of Agriculture SPCS list as the example). |
| Beauty & nail salons | State cosmetology licence lists; Vagaro, Fresha, Booksy | Licence rolls via `state_license`; booking marketplaces are detected on the *website* instead (links to vagaro/fresha/booksy = `booking_provider`), which also answers "phone-only vs online booking". |
| Landscaping, Painting | Google Maps, Thumbtack, Angi | Places + Yelp; Thumbtack/Angi templates in `config/directories.yaml` (disabled: both use bot protection and prohibit scraping; enable only with permission). |
| Remodelers | Houzz | Template in `directories.yaml` (same caveat). Houzz presence is also detected from website links. |
| Janitorial | State business registrations, ISSA directory | Secretary-of-State bulk files can be loaded via `csv_import`; ISSA member directory template. |
| Florists | SAF, FTD and Teleflora directories | SAF template; FTD/Teleflora membership is inferred from website links / domains (`wire_service_only` negative feature). |
| Pet-supply, Gift, Sporting goods | Google Maps, Faire, NSGA | Places + Yelp + OSM; Faire presence detected from website links; NSGA locator template. |
| Women's clothing | Instagram, Shopify store pages | Instagram link + "live sales"/"shop the gram" language on the website; Shopify detected by fingerprint. Direct Instagram scraping is not implemented (Meta ToS); use the Instagram Graph API with business authorisation if needed. |
| Hobby/toy/game | ASTRA directory, publishers' store locators | ASTRA template; Wizards of the Coast / Games Workshop store locators are JSON endpoints that fit the `directory` connector with small adapters. |

**Yelp Fusion** (`yelp`, free tier 500 calls/day): category-filtered search within 40 km,
50 per page; gives a second review count and categories to corroborate Google.

**OpenStreetMap / Overpass** (`osm_overpass`, free, ODbL): craft/shop tags around each cell;
sparse but catches independents and carries `brand` tags that flag chains. Attribute OSM in any
public output.

## 4. Website intelligence and tool stack

`enrich/website.py` fetches the homepage plus up to `ZAHEEN_WEBSITE_MAX_PAGES` internal pages whose
path or anchor mentions contact/about/services/book/shop/events/team/etc., honouring robots.txt
and per-host rate limits, then applies regex rule tables to produce `WebsiteFeatures`.
`enrich/techstack.py` matches Wappalyzer-style fingerprints (`config/tech_fingerprints.yaml`) on
HTML, script URLs, headers and outbound link hosts. **BuiltWith** (`ZAHEEN_BUILTWITH_API_KEY`)
can be layered on top for deeper detection; Wappalyzer's own API is a drop-in alternative.

## 5. Owner contacts (step 4)

Order of precedence in `enrich/owner.py`: vendor record → Apollo people search (titles owner,
founder, president, CEO …) with `people/match` reveal → Hunter domain search → website owner
extraction (schema.org `founder`, "Owner: Jane Doe", "owned and operated by …") plus the least
generic site email. LinkedIn has no public API for this; the export includes LinkedIn URLs when a
vendor/Apollo supplies them, and a company LinkedIn link when the site shows one, for manual lookup.

## Legal and operational checklist

- Google Places: use the API, keep within caching terms, keep place ids as the stable key.
- Vendors: check whether your contract permits API use vs. exports; keep provenance per record
  (`Business.provenance`) so vendor data can be purged on contract end.
- Scrapers: only on public, non-login pages; robots.txt is enforced; identify the bot (`ZAHEEN_USER_AGENT`);
  1 req/s per host by default. Marketplaces with bot protection are disabled by default.
- Outreach: CAN-SPAM (commercial email: accurate headers, physical address, working opt-out),
  TCPA (no autodialed/prerecorded calls or SMS to mobiles without prior express consent; scrub the
  National DNC and state lists), and state privacy laws (CCPA applies to B2B contact data in CA).
  Maintain the suppression list (`zaheen suppress`); exports flag suppressed rows.
