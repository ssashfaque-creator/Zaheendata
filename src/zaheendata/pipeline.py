"""Campaign pipeline: the four-step method from the brief as resumable stages.

    collect  -> long list of 300-500 raw observations from vendors + sub-sector cross-check sources
    resolve  -> entity resolution into unique businesses
    filter   -> remove poor fits (franchise/chain, no website, <10 reviews, outside area)
    enrich   -> reviews (newest), website crawl + features, tech stack, pain analysis; re-filter
    score    -> five 0/1/2 signals, rank, keep top N
    contacts -> owner name / email / phone for the shortlist only (cost control)
    finalize -> stats, cost ledger, campaign state

Each stage persists its output, so a crashed or cancelled run can be resumed from any stage with
``CampaignRunner.run(campaign, start_stage=...)``.
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .enrich import OwnerEnricher, ReviewAnalyzer, TechStackDetector, WebsiteCrawler, extract_features
from .enrich.techstack import BuiltWithClient
from .filters import PoorFitFilter
from .geo import AreaMatcher, Geocoder, GeoPlanner, SearchCell
from .http import HttpClient
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
from .resolve import resolve
from .scoring import ScoringEngine, rank_and_select
from .settings import Settings, get_settings
from .sources import Source, SourceContext, build_sources
from .sources.apollo import ApolloSource
from .sources.google_places import GooglePlacesSource
from .storage import Database

log = get_logger(__name__)

STAGES = ["collect", "resolve", "filter", "enrich", "score", "contacts", "finalize"]
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
        self.builtwith = BuiltWithClient(self.http)

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
        await self.db.clear_raw_records(ctx.campaign.id)
        per_source: Counter[str] = Counter()
        unique_keys: set[str] = set()
        stop = asyncio.Event()
        lock = asyncio.Lock()
        sem = asyncio.Semaphore(max(1, self.settings.global_concurrency // 2))
        emp_min, emp_max = self.settings.employee_min, self.settings.employee_max

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
                employee_min=emp_min,
                employee_max=emp_max,
                limit=max(60, target // max(1, len(ctx.cells))),
            )
            batch: list[RawRecord] = []
            try:
                async with sem:
                    async for rec in source.search(search_spec):
                        batch.append(rec)
                        if len(batch) >= 200:
                            await self._flush(ctx, batch, per_source, unique_keys, lock)
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
            await self._flush(ctx, batch, per_source, unique_keys, lock)
            # Stop expanding once the long list is comfortably above target (resolution shrinks it).
            if len(unique_keys) >= int(target * 1.6):
                stop.set()

        tasks = []
        for source in ctx.sources:
            # Vendors / imports / licence rolls are region-level: one call per distinct region (or once).
            if source.kind.value in ("vendor", "import", "license"):
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
    ) -> None:
        if not batch:
            return
        async with lock:
            await self.db.add_raw_records(ctx.campaign.id, batch)
            for r in batch:
                per_source[r.source] += 1
                unique.add(
                    r.phone
                    or (r.website or "").lower()
                    or f"{r.name.lower()}|{(r.address.postal_code if r.address else '')}"
                )

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
        """The '<10 Google reviews' rule needs review data. Without a maps source (or a vendor file
        carrying review counts) every record would be rejected, so the rule is skipped and flagged."""
        if any(b.review_count is not None for b in businesses):
            return True
        if not ctx.stats.get("review_filter_skipped"):
            log.warning("filter.review_rule_skipped", reason="no source supplied review counts")
            ctx.stats["review_filter_skipped"] = True
        return False

    async def stage_filter(self, ctx: RunContext) -> None:
        businesses = await self.db.list_businesses(ctx.campaign.id)
        area = AreaMatcher(ctx.campaign.spec.geography, ctx.cells)
        require_reviews = self._review_filter_enabled(ctx, businesses)
        reasons = PoorFitFilter(area, self.settings).apply(businesses, require_reviews=require_reviews)
        await self.db.upsert_businesses(businesses)
        remaining = sum(1 for b in businesses if b.status != BusinessStatus.REJECTED)
        ctx.stats["filter_rejections"] = reasons
        ctx.stats["candidates_after_filter"] = remaining
        await self._emit("filter", remaining=remaining, rejected=reasons)

    async def stage_enrich(self, ctx: RunContext) -> None:
        if not ctx.campaign.spec.enrich:
            return
        businesses = await self.db.list_businesses(ctx.campaign.id)
        candidates = [b for b in businesses if b.status != BusinessStatus.REJECTED]
        places = next((s for s in ctx.sources if isinstance(s, GooglePlacesSource)), None)
        sem = asyncio.Semaphore(self.settings.global_concurrency)
        done = 0

        async def enrich_one(b: Business) -> None:
            nonlocal done
            async with sem:
                await self._enrich_business(b, places)
            done += 1
            if done % 25 == 0:
                await self.db.upsert_businesses([b])
                await self._emit("enrich.progress", done=done, total=len(candidates))

        await asyncio.gather(*(enrich_one(b) for b in candidates))
        # Second filter pass: website-derived franchise signals and refreshed review counts.
        area = AreaMatcher(ctx.campaign.spec.geography, ctx.cells)
        require_reviews = self._review_filter_enabled(ctx, businesses)
        reasons = PoorFitFilter(area, self.settings).apply(businesses, require_reviews=require_reviews)
        await self.db.upsert_businesses(businesses)
        remaining = sum(1 for b in businesses if b.status != BusinessStatus.REJECTED)
        ctx.stats["enriched"] = len(candidates)
        ctx.stats["websites_crawled"] = sum(1 for b in candidates if b.website_features.crawled)
        ctx.stats["filter_rejections_after_enrich"] = reasons
        ctx.stats["candidates_after_enrich"] = remaining
        await self._emit("enrich", enriched=len(candidates), remaining=remaining)

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
            if self.builtwith.available and b.domain:
                extra = await self.builtwith.lookup(b.domain)
                known = b.tech_stack.names
                b.tech_stack.technologies.extend(t for t in extra if t.name not in known)
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

    async def stage_contacts(self, ctx: RunContext) -> None:
        if not ctx.campaign.spec.contact_enrichment:
            return
        selected = await self.db.list_businesses(ctx.campaign.id, status=BusinessStatus.SELECTED)
        apollo = next((s for s in ctx.sources if isinstance(s, ApolloSource)), None)
        enricher = OwnerEnricher(self.http, self.settings, apollo)
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
