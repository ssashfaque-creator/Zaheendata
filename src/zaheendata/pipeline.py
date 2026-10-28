"""Campaign pipeline: the four-step method from the brief as resumable stages.

    collect  -> long list from government registries, Yelp, OSM (Google Places is deferred)
    resolve  -> entity resolution into unique businesses
    filter   -> franchise/chain, closed, outside area (website / Google-review rules wait)
    enrich   -> crawl any website we already have; extract all site fields + tech stack
    score    -> five 0/1/2 signals from site + registry data, keep a shortlist
    maps     -> Google Places only on the shortlist (website, reviews, rating); re-score
    contacts -> owner name / email / phone for the shortlist only
    finalize -> stats, cost ledger, campaign state

Each stage persists its output, so a crashed or cancelled run can be resumed from any stage with
``CampaignRunner.run(campaign, start_stage=...)``. Stop after ``score`` for a fully free pass;
resume with ``start_stage=maps`` once a Google key is available.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .enrich import OwnerEnricher, ReviewAnalyzer, TechStackDetector, WebsiteCrawler, extract_features
from .enrich.discover import DirectoryDiscoverer, completeness, harvest_from_website
from .enrich.owner import OwnerLookup
from .filters import PoorFitFilter
from .geo import AreaMatcher, Geocoder, GeoPlanner, SearchCell
from .http import HostPaused, HttpClient
from .logging import get_logger
from .models import (
    Business,
    BusinessStatus,
    Campaign,
    CampaignState,
    RawRecord,
    SourceSearchSpec,
    utcnow,
)
from .registry import SubSector, get_registry
from .resolve import attach_record, resolve
from .scoring import ScoringEngine, rank_and_select
from .settings import Settings, get_settings
from .sources import Source, SourceContext, build_sources
from .sources.google_places import GooglePlacesSource
from .sources.public_datasets import PublicDatasetSource
from .sources.sam_gov import SamGovSource
from .sources.yelp import YelpSource
from .storage import Database

log = get_logger(__name__)

STAGES = ["collect", "resolve", "filter", "enrich", "score", "maps", "contacts", "finalize"]
ProgressHook = Callable[[str, dict[str, Any]], Awaitable[None] | None]


@dataclass
class RunContext:
    campaign: Campaign
    subsector: SubSector
    sources: list[Source]
    cells: list[SearchCell]
    stats: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)


class CampaignRunner:
    def __init__(
        self,
        db: Database,
        http: HttpClient | None = None,
        settings: Settings | None = None,
        extra_sources: list[Source] | None = None,
        progress: ProgressHook | None = None,
    ):
        self.settings = settings or get_settings()
        self.db = db
        self.http = http or HttpClient(self.settings)
        self.extra_sources = extra_sources or []
        self.progress = progress
        self.registry = get_registry()
        self.scoring = ScoringEngine(self.settings)
        self.reviews = ReviewAnalyzer(self.settings)
        self.tech = TechStackDetector()
        self.crawler = WebsiteCrawler(self.http, self.settings)

    async def _emit(self, stage: str, **data: Any) -> None:
        log.info(f"pipeline.{stage}", **data)
        if self.progress:
            result = self.progress(stage, data)
            if asyncio.iscoroutine(result):
                await result

    # ------------------------------------------------------------------ orchestration
    async def run(
        self, campaign: Campaign, start_stage: str = "collect", stop_after: str | None = None
    ) -> Campaign:
        spec = campaign.spec
        subsector = self.registry.get(spec.subsector_key)
        ctx_sources = SourceContext(http=self.http, settings=self.settings)
        sources = build_sources(ctx_sources, subsector, only=spec.sources, extra=self.extra_sources)
        geocoder = Geocoder(self.http)
        cells = await GeoPlanner(geocoder).plan(spec.geography)
        ctx = RunContext(
            campaign=campaign, subsector=subsector, sources=sources, cells=cells, stats=dict(campaign.stats)
        )
        ctx.stats["sources"] = [s.name for s in sources]
        ctx.stats["search_cells"] = len(cells)
        await self.db.update_campaign(campaign.id, state=CampaignState.RUNNING, error=None)

        stage_fns = {
            "collect": self.stage_collect,
            "resolve": self.stage_resolve,
            "filter": self.stage_filter,
            "enrich": self.stage_enrich,
            "score": self.stage_score,
            "maps": self.stage_maps,
            "contacts": self.stage_contacts,
            "finalize": self.stage_finalize,
        }
        try:
            for stage in STAGES[STAGES.index(start_stage) :]:
                await self.db.update_campaign(campaign.id, stage=stage, stats=ctx.stats)
                t0 = time.monotonic()
                await stage_fns[stage](ctx)
                ctx.stats.setdefault("stage_seconds", {})[stage] = round(time.monotonic() - t0, 1)
                await self.db.update_campaign(campaign.id, stats=ctx.stats)
                if stop_after == stage:
                    break
            if stop_after is None or stop_after == "finalize":
                await self.db.update_campaign(campaign.id, state=CampaignState.COMPLETED, stats=ctx.stats)
        except Exception as exc:
            log.exception("pipeline.failed", campaign=campaign.id)
            await self.db.update_campaign(
                campaign.id, state=CampaignState.FAILED, error=str(exc)[:2000], stats=ctx.stats
            )
            raise
        refreshed = await self.db.get_campaign(campaign.id)
        return refreshed or campaign

    # ------------------------------------------------------------------ stages
    async def stage_collect(self, ctx: RunContext) -> None:
        spec = ctx.campaign.spec
        target = spec.long_list_target or self.settings.long_list_target
        cap = max(target, int(target * 1.15))
        await self.db.clear_raw_records(ctx.campaign.id)
        per_source: Counter[str] = Counter()
        unique_keys: set[str] = set()
        stop = asyncio.Event()
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, self.settings.global_concurrency // 2))
        emp_min, emp_max = self.settings.employee_min, self.settings.employee_max
        kind_order = {"registry": 0, "import": 1, "vendor": 1, "license": 2, "maps": 3, "directory": 4}

        async def run_cell(source: Source, cell: SearchCell | None) -> None:
            if stop.is_set():
                return
            search_spec = SourceSearchSpec(
                subsector_key=ctx.subsector.key,
                naics=ctx.subsector.naics,
                queries=ctx.subsector.queries,
                center=cell.center if cell and cell.center.lat != 0 else None,
                radius_km=cell.radius_km if cell else spec.geography.radius_km,
                location_text=cell.label
                if cell and cell.region and "," in cell.label
                else (cell.label if cell else None),
                region=cell.region if cell else None,
                county=cell.county if cell else None,
                employee_min=emp_min,
                employee_max=emp_max,
                limit=max(target, target // max(1, len(ctx.cells))),
            )
            batch: list[RawRecord] = []
            try:
                async with sem:
                    async for rec in source.search(search_spec):
                        batch.append(rec)
                        if len(batch) >= 25:
                            await self._flush(ctx, batch, per_source, unique_keys, lock, cap, stop)
                            batch = []
                        if stop.is_set():
                            break
            except Exception as exc:  # noqa: BLE001 - one source failing must not sink the run
                log.warning(
                    "collect.source_failed",
                    source=source.name,
                    cell=cell.label if cell else None,
                    error=str(exc),
                )
                ctx.stats.setdefault("source_errors", []).append(
                    {"source": source.name, "error": str(exc)[:300]}
                )
            await self._flush(ctx, batch, per_source, unique_keys, lock, cap, stop)

        tasks = []
        ordered = sorted(ctx.sources, key=lambda s: kind_order.get(s.kind.value, 9))
        for source in ordered:
            if self.settings.defer_google_places and source.name in {"google_places", "google_maps_scraper"}:
                ctx.stats.setdefault("collect_skipped_sources", []).append(source.name)
                continue
            # Registries / licence rolls / imports are region-level: one call per distinct region (or once).
            if source.kind.value in ("vendor", "import", "license", "registry"):
                regions = {c.region for c in ctx.cells} or {None}
                for region in regions:
                    rep = next((c for c in ctx.cells if c.region == region), None)
                    tasks.append(run_cell(source, rep))
            else:
                for cell in ctx.cells:
                    tasks.append(run_cell(source, cell))
        await asyncio.gather(*tasks)
        ctx.stats["raw_records"] = sum(per_source.values())
        ctx.stats["raw_by_source"] = dict(per_source)
        ctx.stats["raw_unique_estimate"] = len(unique_keys)
        await self._emit(
            "collect", raw=ctx.stats["raw_records"], unique=len(unique_keys), by_source=dict(per_source)
        )

    async def _flush(
        self,
        ctx: RunContext,
        batch: list[RawRecord],
        per_source: Counter[str],
        unique: set[str],
        lock: asyncio.Lock,
        cap: int,
        stop: asyncio.Event,
    ) -> None:
        if not batch:
            return
        async with lock:
            await self.db.add_raw_records(ctx.campaign.id, batch)
            for r in batch:
                per_source[r.source] += 1
                zipc = (r.address.postal_code if r.address else "") or ""
                unique.add("|".join((r.name.lower(), r.phone or "", (r.website or "").lower(), zipc[:5])))
            if len(unique) >= cap or sum(per_source.values()) >= cap:
                stop.set()

    async def stage_resolve(self, ctx: RunContext) -> None:
        records = await self.db.list_raw_records(ctx.campaign.id)
        trust = {s.name: s.trust for s in ctx.sources}
        businesses = resolve(records, trust)
        for b in businesses:
            b.campaign_id = ctx.campaign.id
            b.naics = b.naics or ctx.subsector.naics
        await self.db.replace_businesses(ctx.campaign.id, businesses)
        ctx.stats["businesses"] = len(businesses)
        await self._emit("resolve", raw=len(records), businesses=len(businesses))

    def _review_filter_enabled(self, ctx: RunContext, businesses: list[Business]) -> bool:
        """The '<10 Google reviews' rule needs review data. Without a maps source (or a file carrying
        review counts) every record would be rejected, so the rule is skipped and flagged."""
        if any(b.review_count is not None for b in businesses):
            return True
        if not ctx.stats.get("review_filter_skipped"):
            log.warning("filter.review_rule_skipped", reason="no source supplied review counts")
            ctx.stats["review_filter_skipped"] = True
        return False

    def _website_filter_enabled(self, ctx: RunContext, businesses: list[Business]) -> bool:
        """Government registries rarely list websites. If no source supplied any, the 'no website' rule
        would empty the campaign, so it is skipped and flagged (the website crawl still runs on nothing)."""
        if any(b.website for b in businesses):
            return True
        if not ctx.stats.get("website_filter_skipped"):
            log.warning("filter.website_rule_skipped", reason="no source supplied websites")
            ctx.stats["website_filter_skipped"] = True
        return False

    @staticmethod
    def _places(ctx: RunContext) -> GooglePlacesSource | None:
        return next((s for s in ctx.sources if isinstance(s, GooglePlacesSource)), None)

    @staticmethod
    def _needs_maps_match(b: Business) -> bool:
        return b.google_place_id is None and (not b.website or b.review_count is None)

    def _missing_website_or_reviews(self, businesses: list[Business]) -> set[str]:
        return {b.id for b in businesses if not b.website or b.review_count is None}

    async def stage_filter(self, ctx: RunContext) -> None:
        businesses = await self.db.list_businesses(ctx.campaign.id)
        area = AreaMatcher(ctx.campaign.spec.geography, ctx.cells)
        # Website and Google-review rules wait until a site is in hand (enrich crawl) or the later
        # maps stage fills them. Franchise / closed / geography still apply now.
        deferred = self._missing_website_or_reviews(businesses)
        ctx.stats["website_review_deferred"] = len(deferred)
        ctx.stats["maps_match_pending"] = len(deferred) if self._places(ctx) else 0
        rest = [b for b in businesses if b.id not in deferred]
        require_reviews = self._review_filter_enabled(ctx, rest) if rest else False
        require_website = self._website_filter_enabled(ctx, rest) if rest else False
        reasons = PoorFitFilter(area, self.settings).apply(
            businesses, require_website=require_website, require_reviews=require_reviews, deferred=deferred
        )
        await self.db.upsert_businesses(businesses)
        remaining = sum(1 for b in businesses if b.status != BusinessStatus.REJECTED)
        ctx.stats["filter_rejections"] = reasons
        ctx.stats["candidates_after_filter"] = remaining
        await self._emit("filter", remaining=remaining, rejected=reasons)

    @staticmethod
    def _place_parts(b: Business, fallback: str) -> tuple[str | None, str | None]:
        addr = b.address
        city = addr.city if addr else None
        region = addr.region if addr else None
        if (not city or not region) and "," in fallback:
            left, right = (p.strip() for p in fallback.split(",", 1))
            city = city or left
            region = region or (right.split()[0] if right else None)
        return city, region

    async def stage_enrich(self, ctx: RunContext) -> None:
        """Find missing websites/addresses for free, then crawl every site we have. No Google Places."""
        if not ctx.campaign.spec.enrich:
            return
        businesses = await self.db.list_businesses(ctx.campaign.id)
        candidates = [b for b in businesses if b.status != BusinessStatus.REJECTED]
        candidates.sort(
            key=lambda b: (
                0 if b.website else 1,
                0 if (b.address and b.address.line1) else 1,
                0 if b.phone_e164 else 1,
                b.name.lower(),
            )
        )
        pool = ctx.campaign.spec.long_list_target or self.settings.long_list_target
        candidates = candidates[:pool]
        places = None if self.settings.defer_google_places else self._places(ctx)
        sam = next((s for s in ctx.sources if isinstance(s, SamGovSource)), None)
        yelp = next((s for s in ctx.sources if isinstance(s, YelpSource)), None)
        discoverer = DirectoryDiscoverer(self.http, self.settings)
        locs = ctx.campaign.spec.geography.locations
        discoverer.fallback_where = locs[0] if locs else (ctx.campaign.spec.geography.state or "")
        sem = asyncio.Semaphore(max(4, self.settings.global_concurrency // 2))
        done = 0
        discovered = 0
        budget = min(self.settings.website_discover_limit or pool, pool)
        pending: list[Business] = []
        pending_lock = asyncio.Lock()

        async def enrich_one(b: Business) -> None:
            nonlocal done, discovered
            async with sem:
                needs = not b.website or not (b.address and b.address.line1) or not b.phone_e164
                if needs and discovered < budget:
                    discovered += 1
                    city, region = self._place_parts(b, discoverer.fallback_where)
                    if sam is not None:
                        try:
                            rec = await sam.find_entity(b.name, region)
                            if rec is not None:
                                attach_record(b, rec)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("enrich.sam_failed", business=b.name, error=str(exc)[:200])
                    if yelp is not None and (not b.phone_e164 or b.review_count is None):
                        try:
                            rec = await yelp.match_business(b.name, city, region)
                            if rec is not None:
                                attach_record(b, rec)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("enrich.yelp_failed", business=b.name, error=str(exc)[:200])
                    await discoverer.fill(b)
                await self._enrich_business(b, places)
                harvest_from_website(b)
            async with pending_lock:
                pending.append(b)
                done += 1
                flush = pending[:] if len(pending) >= 10 else []
                if flush:
                    pending.clear()
                progress = done
            if flush:
                await self.db.upsert_businesses(flush)
            if progress % 10 == 0:
                await self._emit("enrich.progress", done=progress, total=len(candidates))

        await asyncio.gather(*(enrich_one(b) for b in candidates))
        if pending:
            await self.db.upsert_businesses(pending)
        # Re-check franchise language discovered on the site. Website / review cuts stay deferred
        # for records that still lack a URL or a review count.
        area = AreaMatcher(ctx.campaign.spec.geography, ctx.cells)
        alive = [b for b in businesses if b.status != BusinessStatus.REJECTED]
        still_missing = self._missing_website_or_reviews(alive)
        rest = [b for b in alive if b.id not in still_missing]
        require_reviews = self._review_filter_enabled(ctx, rest) if rest else False
        require_website = self._website_filter_enabled(ctx, rest) if rest else False
        reasons = PoorFitFilter(area, self.settings).apply(
            businesses,
            require_website=require_website,
            require_reviews=require_reviews,
            deferred=still_missing or None,
        )
        await self.db.upsert_businesses(businesses)
        remaining = sum(1 for b in businesses if b.status != BusinessStatus.REJECTED)
        ctx.stats["enriched"] = len(candidates)
        ctx.stats["websites_discovered"] = sum(1 for b in candidates if b.attributes.get("website_source"))
        ctx.stats["websites_crawled"] = sum(1 for b in candidates if b.website_features.crawled)
        ctx.stats["filter_rejections_after_enrich"] = reasons
        ctx.stats["candidates_after_enrich"] = remaining
        await self._emit(
            "enrich",
            enriched=len(candidates),
            remaining=remaining,
            crawled=ctx.stats["websites_crawled"],
            discovered=ctx.stats["websites_discovered"],
        )

    async def _match_maps(self, b: Business, places: GooglePlacesSource) -> bool:
        """Attach the Google Maps listing to a registry-sourced business (website, phone, reviews)."""
        addr = b.address
        try:
            rec = await places.match_business(
                b.name,
                addr.city if addr else None,
                addr.region if addr else None,
                addr.postal_code if addr else None,
            )
        except HostPaused:
            b.attributes["maps_match"] = "rate_limited"
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("enrich.maps_match_failed", business=b.name, error=str(exc)[:200])
            return False
        if rec is None:
            b.attributes["maps_match"] = "none"
            return False
        attach_record(b, rec)
        b.attributes["maps_match"] = "matched"
        return True

    async def _enrich_business(self, b: Business, places: GooglePlacesSource | None) -> None:
        if places is not None and b.google_place_id and not b.reviews:
            try:
                reviews, meta = await places.fetch_reviews(b.google_place_id)
                b.reviews = reviews
                b.rating = meta.get("rating") or b.rating
                b.review_count = meta.get("review_count") or b.review_count
                b.business_status = meta.get("business_status") or b.business_status
            except Exception as exc:  # noqa: BLE001
                log.debug("enrich.reviews_failed", business=b.name, error=str(exc))
        b.review_insights = self.reviews.analyse(b.reviews)
        if b.website:
            try:
                pages = await self.crawler.crawl(b.website)
                b.website_features = extract_features(pages, b.domain)
                b.tech_stack = self.tech.detect(pages, b.domain)
            except Exception as exc:  # noqa: BLE001
                b.website_features.error = str(exc)[:200]
        b.updated_at = utcnow()

    async def stage_score(self, ctx: RunContext) -> None:
        businesses = await self.db.list_businesses(ctx.campaign.id)
        top_n = ctx.campaign.spec.short_list_target or self.settings.short_list_target
        for b in businesses:
            if b.status != BusinessStatus.REJECTED:
                b.score = self.scoring.score(b, ctx.subsector)
        selected = rank_and_select(businesses, top_n)
        await self.db.upsert_businesses(businesses)
        dist = Counter(b.score.total for b in businesses if b.score)
        ctx.stats["scored"] = sum(1 for b in businesses if b.score)
        ctx.stats["selected"] = len(selected)
        ctx.stats["score_distribution"] = {str(k): v for k, v in sorted(dist.items(), reverse=True)}
        await self._emit(
            "score",
            scored=ctx.stats["scored"],
            selected=len(selected),
            distribution=ctx.stats["score_distribution"],
        )

    def _maps_pool(self, businesses: list[Business], top_n: int) -> list[Business]:
        """Shortlist plus a small overflow so Maps can replace leads that fail the review/website cut."""
        ranked = [b for b in businesses if b.status != BusinessStatus.REJECTED]
        ranked.sort(
            key=lambda b: (
                0 if b.status == BusinessStatus.SELECTED else 1,
                -(b.score.total if b.score else 0),
                *tuple(-part for part in completeness(b)),
                b.name.lower(),
            )
        )
        cap = max(top_n, top_n * self.settings.google_places_shortlist_multiplier)
        return ranked[:cap]

    async def stage_maps(self, ctx: RunContext) -> None:
        """Google Places on the shortlist only: attach listing, crawl a newly found website, fetch reviews."""
        places = self._places(ctx)
        if places is None:
            businesses = await self.db.list_businesses(ctx.campaign.id)
            self._website_filter_enabled(ctx, businesses)
            self._review_filter_enabled(ctx, businesses)
            ctx.stats["maps_stage"] = "skipped"
            await self._emit("maps", skipped="no Places source")
            return
        businesses = await self.db.list_businesses(ctx.campaign.id)
        top_n = ctx.campaign.spec.short_list_target or self.settings.short_list_target
        pool = self._maps_pool(businesses, top_n)
        ctx.stats["maps_pool"] = len(pool)
        sem = asyncio.Semaphore(1)  # Places is already paced inside the client; keep lookups serial
        matched = 0
        done = 0
        places_host = "places.googleapis.com"

        async def one(b: Business) -> None:
            nonlocal matched, done
            async with sem:
                if self._needs_maps_match(b):
                    if self.http.is_paused(places_host):
                        ctx.stats["maps_rate_limited"] = True
                        b.attributes.setdefault("maps_match", "rate_limited")
                    elif await self._match_maps(b, places):
                        matched += 1
                await self._enrich_business(b, places)
                harvest_from_website(b)
            done += 1
            if done % 10 == 0:
                await self.db.upsert_businesses([b])
                await self._emit("maps.progress", done=done, total=len(pool), matched=matched)

        await asyncio.gather(*(one(b) for b in pool))
        attempted = {b.id for b in pool if b.attributes.get("maps_match") in {"matched", "none"}}
        deferred = {b.id for b in businesses if b.id not in attempted}
        area = AreaMatcher(ctx.campaign.spec.geography, ctx.cells)
        looked_up = [b for b in pool if b.id in attempted]
        require_reviews = self._review_filter_enabled(ctx, looked_up) if looked_up else False
        require_website = self._website_filter_enabled(ctx, looked_up) if looked_up else False
        reasons = PoorFitFilter(area, self.settings).apply(
            businesses,
            require_website=require_website,
            require_reviews=require_reviews,
            deferred=deferred or None,
        )
        for b in businesses:
            if b.status != BusinessStatus.REJECTED:
                b.score = self.scoring.score(b, ctx.subsector)
        selected = rank_and_select(businesses, top_n)
        await self.db.upsert_businesses(businesses)
        ctx.stats["maps_matched"] = matched
        ctx.stats["filter_rejections_after_maps"] = reasons
        ctx.stats["selected"] = len(selected)
        ctx.stats["maps_stage"] = "ok"
        await self._emit("maps", pool=len(pool), matched=matched, selected=len(selected), rejected=reasons)

    async def stage_contacts(self, ctx: RunContext) -> None:
        if not ctx.campaign.spec.contact_enrichment:
            return
        selected = await self.db.list_businesses(ctx.campaign.id, status=BusinessStatus.SELECTED)
        lookups: list[tuple[str, OwnerLookup]] = []
        sam = next((s for s in ctx.sources if isinstance(s, SamGovSource)), None)
        if sam is not None:
            lookups.append(("sam_gov", sam.find_owner))
        registries = next((s for s in ctx.sources if isinstance(s, PublicDatasetSource)), None)
        if registries is not None:
            lookups.append(("state_registry", registries.lookup_owner))
        enricher = OwnerEnricher(self.http, self.settings, lookups)
        sem = asyncio.Semaphore(max(2, self.settings.global_concurrency // 4))

        async def one(b: Business) -> None:
            async with sem:
                b.owner = await enricher.enrich(b)
                # owner_reachable may improve now that contacts exist
                b.score = self.scoring.score(b, ctx.subsector)

        await asyncio.gather(*(one(b) for b in selected))
        # Re-rank within the shortlist only (membership is fixed by the earlier selection).
        selected.sort(key=lambda b: (-(b.score.total if b.score else 0), -(b.review_count or 0)))
        for i, b in enumerate(selected, start=1):
            if b.score:
                b.score.rank = i
        await self.db.upsert_businesses(selected)
        ctx.stats["owners_named"] = sum(1 for b in selected if b.owner.name)
        ctx.stats["owner_emails"] = sum(1 for b in selected if b.owner.email)
        await self._emit(
            "contacts",
            shortlist=len(selected),
            named=ctx.stats["owners_named"],
            emails=ctx.stats["owner_emails"],
        )

    async def stage_finalize(self, ctx: RunContext) -> None:
        ctx.stats["status_counts"] = await self.db.business_counts(ctx.campaign.id)
        ctx.stats["cost"] = self.http.ledger.summary()
        ctx.stats["duration_seconds"] = round(time.monotonic() - ctx.started, 1)
        ctx.stats["finished_at"] = utcnow().isoformat()
        await self._emit(
            "finalize", **{k: ctx.stats[k] for k in ("status_counts", "cost", "duration_seconds")}
        )
