# Architecture

```
                 ┌──────────────────────── CampaignRunner (pipeline.py) ────────────────────────┐
                 │ collect → resolve → filter → enrich → score → contacts → finalize            │
                 └──────────────────────────────────────────────────────────────────────────────┘
   GeoPlanner            Sources              resolve.py     filters.py   enrich/*      scoring.py   export.py
   (geo.py)        (sources/registry.py)      union-find     franchise    crawler       5 signals    CSV/XLSX/JSON
   metro / state     google_places, yelp,     phone/domain/  website      fingerprints  rank/top-N   compliance
   national grids    osm, apollo, data_axle,  place-id +     reviews      reviews       evidence     flags
                     state_license, directory fuzzy name     area         owner
                     csv_import, scraper
                              │
                        HttpClient (http.py): per-host token bucket + semaphores, retries w/ Retry-After,
                        SQLite response cache (TTL), robots.txt, cost ledger
                              │
                        Database (storage.py): campaigns, raw_records, businesses, suppressions
                        SQLite (dev) / Postgres (prod) via SQLAlchemy async
                              │
                 CLI (cli.py)  ·  FastAPI (api.py)  ·  Worker loop (cli.py worker)
```

## Modules

| Module | Responsibility |
|---|---|
| `settings.py` | Typed configuration (`ZAHEEN_*` env / `.env`), thresholds from the brief, feature flags, keys. |
| `models.py` | Pydantic domain: `RawRecord` (one observation from one source), `Business` (resolved entity with enrichment, score, provenance), `CampaignSpec`/`Campaign`, `GeographySpec`, `SourceSearchSpec`. |
| `registry.py` + `config/subsectors.yaml` | The 17 sub-sectors with NAICS, scope, cross-check sources, queries and fit-signal rules. |
| `geo.py` | State bounding boxes, grid generation for state/national sweeps, geocoding (Google or Nominatim), area membership. |
| `http.py` | Polite HTTP: concurrency limits, token buckets, retry/back-off, on-disk cache, robots.txt, `CostLedger`. |
| `sources/` | Connector interface (`Source.search(spec) -> AsyncIterator[RawRecord]`) and implementations. Sources declare `tags` that match `cross_check` entries so each campaign automatically uses the sources the brief recommends; `always_on` sources (Places, vendors, OSM) run for every sub-sector when configured. |
| `normalize.py` | E.164 phones, canonical URLs/domains, legal-suffix-free names, US address completion. |
| `resolve.py` | Entity resolution: blocking on phone / domain / place id, fuzzy name matching within ZIP/city/geo blocks, union-find clustering, trust-based field merge with provenance. |
| `filters.py` | Poor-fit rules and `FranchiseDetector` (brand lists + structural signals across the candidate set). |
| `enrich/website.py` | Bounded crawler + regex feature extraction (`WebsiteFeatures`). |
| `enrich/techstack.py` | Fingerprint detection + BuiltWith client. |
| `enrich/reviews.py` | Recency + admin-pain classification (pluggable classifier). |
| `enrich/owner.py` | Owner contact waterfall (vendor → Apollo → Hunter → website). |
| `scoring.py` | Five signals, feature resolver for YAML rules, ranking and top-N selection. |
| `compliance.py` | Suppression evaluation, role-account / free-mail flags, channel notes. |
| `export.py` | Flat row schema and CSV/XLSX/JSON writers. |
| `storage.py` | SQLAlchemy models and repository methods; `claim_queued_campaign` for workers. |
| `pipeline.py` | Stage orchestration, concurrency, persistence between stages, stats and resume. |
| `api.py` / `cli.py` | Operator surfaces. |
| `testing.py` | `FixtureTransport` so the full pipeline runs offline. |

## Pipeline semantics

- **collect** fans out `sources × cells`. Maps-type sources run per geographic cell; vendors,
  imports and licence rolls run once per region. Raw records are flushed to `raw_records` in
  batches; collection stops expanding once unique keys exceed 1.6 × long-list target. One failing
  source is logged in `stats.source_errors` and never aborts the run.
- **resolve** rebuilds `businesses` from all raw records (idempotent).
- **filter / enrich / score / contacts** update businesses in place; status moves
  `candidate → rejected | scored | selected`. Contact enrichment (the paid part) runs only for the
  selected shortlist, then re-ranks within it.
- Every stage writes `campaign.stage` and `campaign.stats`, so `run(start_stage=...)` resumes
  after a crash or after changing thresholds without re-collecting (and re-paying).

## Scaling and operations

- **Horizontal workers:** `zaheen worker` claims `QUEUED` campaigns with `FOR UPDATE SKIP LOCKED`
  on Postgres; add replicas in `docker-compose.yml` or a Kubernetes Deployment.
- **Concurrency:** `ZAHEEN_GLOBAL_CONCURRENCY`, `ZAHEEN_PER_HOST_CONCURRENCY`,
  `ZAHEEN_PER_HOST_REQUESTS_PER_SECOND`; sources can override per host (`HttpClient.set_host_rate`).
- **Cost control:** the ledger counts paid calls per meter and estimates spend; the response
  cache prevents repeat billing; contacts run on the shortlist only; `stop_after="filter"` lets you
  review the long list before paying for enrichment.
- **Observability:** structlog (console or JSON), per-stage timings, per-source counts, rejection
  reasons, score distribution in `campaign.stats`; `/health` for probes.
- **Multi-process cache:** the SQLite response cache is per host; for many workers point
  `ZAHEEN_HTTP_CACHE_PATH` at a shared volume or swap `ResponseCache` for a Redis implementation
  (`ZAHEEN_REDIS_URL` is reserved for this).
- **Schema evolution:** tables are created with `create_all`; introduce Alembic before the first
  production schema change.

## Extending

- **New source:** subclass `Source`, set `name`, `kind`, `tags`, `trust`, implement `search`, add to
  `SOURCE_CLASSES`. Tag names link it to `cross_check` lists in `subsectors.yaml`.
- **New sub-sector:** add an entry to `subsectors.yaml`; if its signal needs a new website feature,
  add a field to `WebsiteFeatures` and a pattern in `enrich/website.py`.
- **New franchise / technology:** edit the YAML lists.
- **LLM-assisted classification:** pass `classifier=` to `ReviewAnalyzer`, or post-process
  `WebsiteFeatures` from page text; the deterministic rules remain the baseline and the tests.
