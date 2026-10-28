# Data sources: what we need, where it comes from, how it is obtained

The engine uses **no paid list vendor**. The role a vendor plays in the brief (a long list filtered
by NAICS, employee band and geography, with an owner name) is covered by free government data;
Google Maps supplies presence, website and reviews; the business's own website supplies the rest.

| Need | Fields | Primary source | Cross-check / fallback |
|---|---|---|---|
| Long list by NAICS + 5–19 employees + area | legal name, NAICS, address, employee count, franchise flag, owner name | SBA PPP loan data, SAM.gov, state/city NAICS-coded registries (Socrata) | Google Places text search, Yelp, OSM |
| Google Maps presence and reviews | place id, rating, review count, newest reviews, status, website, phone | Google Places API (New) + Place Details | Yelp Fusion, OSM |
| Website intelligence | booking/quote intake, plans, services, gift cards, store, events, owner name, emails | our crawler | – |
| Tool stack | payments, e-commerce, booking, email marketing, CRM, field-service software | Wappalyzer-style fingerprints on crawled HTML, scripts, headers, outbound hosts | – |
| Owner contact | name, title, email, direct phone, LinkedIn | registry record → SAM.gov POC → Secretary of State / L&I principal → website | LinkedIn (manual) |
| Geography sizing | establishments per county/metro/state by size class | Census County Business Patterns | – |

All of these are free; only Google Places is metered (tracked in the cost ledger).

## 1. Government registries (step 1: the long list)

### SBA Paycheck Protection Program FOIA data (`sba_ppp`)

The single best free substitute for a list vendor. The SBA published every PPP loan (≈11.5M rows)
with `BorrowerName`, address, `NAICSCode`, **`JobsReported`** (the only nationwide, free employee
count), `FranchiseName`, `BusinessType`, `BusinessAgeDescription` and `NonProfit`.

- Download: `zaheen datasets ppp-download` (lists and fetches the `public_*.csv` files from
  data.sba.gov's DKAN API, ≈8 GB) or copy the CSVs into `ZAHEEN_PPP_DATA_DIR`.
- First use builds `<ppp_data_dir>/_by_state/<ST>.csv` once (one streaming pass over every file);
  each search then reads a single state file in a worker thread.
- Filters: NAICS prefix, borrower state, borrower city (metro campaigns), `JobsReported` within
  5–19, drops non-profits, dedupes first/second-draw loans by name + ZIP. `FranchiseName` becomes
  the franchise flag that the poor-fit filter rejects on. Sole proprietors / independent
  contractors are kept but marked `individual_structure`.
- Caveats: a 2020–21 snapshot (businesses founded since are missing; some have closed, which the
  Maps status check catches); covers roughly 60–70% of employer firms in the target band.

### SAM.gov Entity Management API (`sam_gov`, free key from api.data.gov)

Every entity registered to work with the federal government: legal and DBA name, physical
address, website, entity structure, NAICS codes and **points of contact** (name + title;
email/phone need a FOUO-entitled key). Small service firms (janitorial, landscaping, painting,
electrical, HVAC, pest control) register frequently. Queried by `naicsCode` + state (+ city) for the
long list, and by `legalBusinessName` / `dbaName` during contact enrichment (`find_owner`).

### State and city NAICS-coded registries (`public_datasets`, Socrata)

Configured in `config/public_datasets.yaml`; every entry was verified against the live endpoint
and `zaheen datasets check` re-verifies ids, columns and filters before a production run.

| Dataset | Covers | NAICS | Extras |
|---|---|---|---|
| Texas Comptroller sales-tax permits | all TX outlets | numeric `outlet_naics_code` | taxpayer (permit holder) name, city/state/zip |
| Los Angeles active businesses | LA city | `naics` | DBA + legal name |
| San Francisco registered businesses | SF | self-reported (2-digit prefix) | `ownership_name` |
| Seattle business licence tax certificates | Seattle | `naics_code` | phone, trade name, ownership type |
| Berkeley business licences | Berkeley | `naics` | **employee count** |
| East Baton Rouge businesses | EBR Parish | numeric | phone, website |
| Connecticut business registry | all CT | "Name (code)" text | **business email**, entity type |
| Chicago business licences | Chicago | keyword on activity | lat/lng |

Narrowing is done server-side with SoQL: `starts_with()` / `=` / range / `like` on the NAICS
column depending on `naics_match`, `upper(city) = ...` for metro campaigns, and – for datasets
without a city column such as TDLR – `upper(county) like ...` using the county returned by the
geocoder. A dataset with neither is capped so one statewide roll cannot swamp the long list.

### Adding a registry

Search the Socrata catalogue (`api.us.socrata.com/api/catalog/v1?q=business+license+NAICS`),
data.gov, a state portal or a Secretary of State bulk download, copy the closest entry in
`public_datasets.yaml`, map the columns, and run `zaheen datasets check <key>`. Bulk files that
need a manual download (Florida DBPR licensee files, California CSLB data portal, most Secretary of
State extracts) are merged into any campaign with `zaheen campaign run <id> --import file.csv`
(columns auto-map).

## 2. Google Maps data

### Google Places API (New) — the compliant route (`google_places`)

- **Text Search** `POST places:searchText` with `textQuery` ("plumber in Austin, TX") and a
  `locationBias` circle per search cell; 20 results per page, 3 pages, field mask limited to what
  we store. ≈ $32 per 1,000 requests.
- **Deferred until the shortlist.** Collect and enrich do not call Places (`ZAHEEN_DEFER_GOOGLE_PLACES`,
  default on). The engine crawls any website already on the registry record, scores, and keeps the
  top N. The **maps** stage then looks up only that shortlist (plus a small overflow,
  `short_list_target × ZAHEEN_GOOGLE_PLACES_SHORTLIST_MULTIPLIER`) by name + city + state. A
  candidate is accepted when the name is a close fuzzy match (`token_set_ratio ≥ 82`) and the
  state agrees. Newly found websites are crawled immediately; then the website and `<10 reviews`
  rules run on those lookups and the shortlist is re-ranked. Cost: ~200 text searches for a
  100-lead shortlist, not thousands. Free pass: `zaheen campaign run <id> --stop-after score`,
  later `zaheen campaign run <id> --start-stage maps`.
- **Place Details (legacy endpoint)** for `reviews` with `reviews_sort=newest`, `rating`,
  `user_ratings_total`, `business_status`. ≈ $20 per 1,000.
- Responses are cached on disk (`ZAHEEN_HTTP_CACHE_TTL_SECONDS`, default 7 days).
- **Terms:** Places data may be cached for 30 days except place ids (indefinitely). Do not display
  it on non-Google maps.

Without a Maps key the engine still runs. During **enrich** it looks up missing websites and
addresses for free: OpenStreetMap Nominatim, SAM.gov (if keyed), Yelp Fusion (free tier, if keyed),
then DuckDuckGo HTML search. The first official-looking site is crawled for booking, tools, owner
name, email and phone so scoring can rank real leads before any Google spend. Website and review
*exclusion* rules still wait for the maps stage (or are skipped and flagged).

### Google Maps scraper (`google_maps_scraper`, Playwright) — experimental, off by default

Google's Terms of Service prohibit automated access; provided only for organisations that have
made their own legal assessment. Enable with `ZAHEEN_ENABLE_GOOGLE_MAPS_SCRAPER=true`.

## 3. Sub-sector cross-check sources (step 1b)

| Sub-sector | Cross-check in the brief | How the engine gets it |
|---|---|---|
| Plumbing/HVAC, Electrical, Roofing, Remodelers, Painting | State contractor / electrical licence lists | `public_datasets` tags `state_contractor_license`, `state_electrical_license`: Texas TDLR (A/C and electrical contractors, narrowed by county), Washington L&I registered contractors (specialty, phone, principal), Oregon BCD (plumbing/electrical/HVAC contractors), Delaware DPR (master plumbers/electricians/HVACR), NYC DCWP home-improvement contractors. |
| Pest control | State pest-control licences | Tag `state_pest_control_license`; no verified open-data roll yet – import the state file with `--import`. SBA PPP covers NAICS 561710 directly. |
| Beauty & nail salons | State cosmetology licence lists; Vagaro, Fresha, Booksy | TDLR salon licences, Delaware cosmetology establishments; booking marketplaces are detected on the website (`booking_provider`). |
| Landscaping, Painting | Google Maps, Thumbtack, Angi | Places + Yelp; Thumbtack/Angi templates in `config/directories.yaml` (disabled: bot protection / ToS). |
| Janitorial | State business registrations, ISSA directory | SAM.gov (janitorial firms register heavily), CT/TX registries, ISSA member directory template. |
| Florists | SAF, FTD and Teleflora directories | SAF template; FTD/Teleflora inferred from website links (`wire_service_only`). |
| Pet-supply, Gift, Sporting goods | Google Maps, Faire, NSGA | Places + Yelp + OSM + PPP; Faire detected from website links; NSGA locator template. |
| Women's clothing | Instagram, Shopify store pages | Instagram link + live-sale language on the website; Shopify by fingerprint. |
| Hobby/toy/game | ASTRA directory, publishers' store locators | ASTRA template; publisher store locators fit the `directory` connector. |

**Yelp Fusion** (`yelp`, free tier): category-filtered search, second review count.
**OpenStreetMap / Overpass** (`osm_overpass`, free, ODbL): craft/shop tags; `brand` tags flag chains.

## 4. Geography sizing: Census County Business Patterns (`zaheen plan`)

`GET api.census.gov/data/2022/cbp` with `NAICS2017=<code>`, `LFO=001`, and one `EMPSZES` code per
request (`001` all / `220` 5–9 / `230` 10–19 employees) by county, state or metro. Wildcards in
`for`/`in` (`county:*`) are sent unencoded — Census returns HTTP 204 if they are percent-encoded.
`zaheen plan florists --state TX` and `GET /plan/florists?state=TX` rank geographies by 5–19
employee establishments and recommend `local`, `local_tight`, `state` or `national` scope before
any crawl budget is spent. Requires the free key (`ZAHEEN_CENSUS_API_KEY`).

## 5. Website intelligence and tool stack

`enrich/website.py` fetches the homepage plus up to `ZAHEEN_WEBSITE_MAX_PAGES` internal pages,
honouring robots.txt and per-host rate limits, and applies regex rule tables to produce
`WebsiteFeatures`. `enrich/techstack.py` matches Wappalyzer-style fingerprints
(`config/tech_fingerprints.yaml`) on HTML, script URLs, headers and outbound link hosts – no paid
technology-lookup API is used.

## 6. Owner contacts (step 4) — free waterfall

`enrich/owner.py`, shortlist only:

1. **Owner already on a record**: licence holder (TDLR `owner_name`), SF `ownership_name`, Texas
   permit holder, Seattle/Berkeley owner fields, SAM.gov POC collected during the long list,
   Washington L&I `primaryprincipalname`.
2. **SAM.gov lookup** by legal/DBA name → POC name and title (email/phone with a FOUO key).
3. **Officer / principal registries** (`owner_lookup` datasets): New York DOS chairman name,
   Colorado SoS registered agent, Washington L&I principals; fuzzy name match ≥ 88 within the
   business's state. "Last, First" names are normalised.
4. **Website**: owner byline ("Owner: Jane Doe", schema.org founder), the least generic mailbox
   (role accounts such as info@ are flagged), a personal LinkedIn profile link; the business
   phone is the fallback phone.

Each contact carries `source` and `confidence` so the export can be filtered.

## Legal and operational checklist

- Google Places: use the API, keep within caching terms, keep place ids as the stable key.
- Government data: public domain / open licence; keep `Business.provenance` so every field can be
  traced to its registry. SBA PPP and SAM.gov data must not be used to make credit decisions.
- Scrapers: only public, non-login pages; robots.txt enforced; identified bot; 1 req/s per host.
- Outreach: CAN-SPAM (accurate headers, physical address, working opt-out), TCPA (no autodialed
  or prerecorded calls/SMS to mobiles without consent; scrub National DNC and state lists), state
  privacy laws. Maintain the suppression list (`zaheen suppress`); exports flag suppressed rows.
