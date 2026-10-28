# Scoring reference

Implemented in `src/zaheendata/scoring.py`; thresholds in `settings.py` (env-overridable);
sub-sector rules in `config/subsectors.yaml`. Each signal is 0, 1 or 2; total 0–10. Ties are
broken by Google review count. The export includes a written `score_evidence` column so every
point is auditable.

## Poor-fit filters (applied before scoring, `filters.py`)

| Reason | Rule |
|---|---|
| closed | Google `businessStatus` is CLOSED_PERMANENTLY / CLOSED_TEMPORARILY (Yelp `is_closed`) |
| franchise/chain | brand list or domain list match (`config/franchises.yaml`); vendor franchise/chain flag; OSM `brand:wikidata`; same domain or phone shared by ≥ 4 businesses in the candidate set; website franchise language without "locally owned" language; website mentions ≥ 4 locations |
| no website | no first-party website (social/directory URLs do not count) |
| too few reviews | Google review count < `ZAHEEN_MIN_GOOGLE_REVIEWS` (10) |
| outside target area | metro: farther than radius × 1.15 from every search cell, or state mismatch; state: state mismatch |
| employee count out of range | opt-in (`ZAHEEN_ENFORCE_EMPLOYEE_RANGE`), only when a vendor supplied a count |

Filters run twice: after entity resolution and again after enrichment (website-derived franchise
signals, refreshed review counts).

## Signals

### busy_and_growing — "50+ Google reviews, with recent ones"

| Points | Condition |
|---|---|
| 0 | review count < 50 |
| 1 | ≥ 50 reviews but fewer than 3 reviews in the last 90 days (or reviews not fetched) |
| 2 | ≥ 50 reviews and ≥ 3 of the newest reviews are within 90 days |

### admin_pain — "reviews mention slow callbacks or billing problems; site says 'call for a quote' with no online booking or form"

Points accumulate (capped at 2):

- +1 if any review matches the pain patterns (`enrich/reviews.py`: slow_callback, billing,
  scheduling, quote_estimate); +1 more if ≥ 2 reviews match.
- +1 if the website says "call for a quote / pricing" and has no online booking and no quote form;
  or if it has no booking, quote form or contact form at all.

### multi_tool — "the tech stack shows a mix of separate tools"

Counts distinct *business tool* categories (payments, ecommerce, booking, field_service,
email_marketing, crm, reputation, live_chat, forms, accounting, gift_cards, financing,
call_tracking, events, live_selling, wholesale). CMS, analytics and social links do not count.

| Points | Categories |
|---|---|
| 0 | ≤ 1 |
| 1 | 2 |
| 2 | ≥ 3 |

### owner_reachable — "owner named on the site, LinkedIn or Google profile"

| Points | Condition |
|---|---|
| 0 | no owner name |
| 1 | owner named (PPP/licence record, SAM.gov, Secretary of State filing, website) |
| 2 | named **and** a direct channel: non-role email (not info@/contact@…), a phone different from the main line, or a personal LinkedIn profile (`/in/`) |

### subsector_signal — the extra signal per sub-sector

`points = 2` when matched features ≥ `strong_threshold`, `1` when ≥ 1, else `0`. A matched
negative feature with no positive match forces 0; with positives it caps the score at 1.

| Sub-sector | Features (from the brief) | Negative |
|---|---|---|
| Plumbing/HVAC | emergency_service, maintenance_plan | |
| Electrical | residential_focus, emergency_service | commercial_only |
| Landscaping | recurring_service, multiple_crews_or_staff | |
| Residential remodelers | project_gallery, free_estimate, has_quote_form | |
| Roofing | inspection_request, insurance_storm_work | |
| Pest control | recurring_service, maintenance_plan | |
| Painting | residential_focus, free_estimate | |
| Other building services | recurring_service, maintenance_plan (threshold 1) | |
| Beauty salons | five_plus_chairs, retail_products, no_online_booking | |
| Nail salons | walk_ins_welcome, sells_gift_cards, no_online_booking | |
| Janitorial | commercial_clients, multiple_crews_or_staff | |
| Florists | wedding_event_work, online_store | wire_service_only |
| Pet-supply | grooming_services, retail_products, has_online_booking | |
| Sporting goods | team_uniform_sales, has_quote_form | |
| Gift & souvenir | store_on_shopify_or_etsy (threshold 1) | |
| Women's clothing | instagram_shop_signals, instagram_present, online_store | |
| Hobby/toy/game | hosts_events, online_store | |

Feature names resolve against `WebsiteFeatures` fields, derived features (`no_online_booking`,
`five_plus_chairs`, `store_on_shopify_or_etsy`, `instagram_present`, `commercial_only`,
`wire_service_only`, `busy_50_reviews`) or prefixed lookups (`tech:<category|name>`,
`social:<network>`, `attr:<attribute>`), so new rules can be written in YAML without code.

## Tuning

Refine on the first 100 for a pilot sub-sector as the brief suggests: adjust thresholds via
environment variables, add brands to `franchises.yaml`, add phrases to the regex tables in
`enrich/website.py` / `enrich/reviews.py`, or plug an LLM classifier into
`ReviewAnalyzer(classifier=...)` for higher-recall pain detection. The `score_evidence` column
shows exactly why each point was awarded.
