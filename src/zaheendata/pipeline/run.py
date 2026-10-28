from __future__ import annotations

import asyncio
from pathlib import Path

from zaheendata.cache import JsonCache
from zaheendata.models import Lead
from zaheendata.pipeline.crawl import crawl_leads
from zaheendata.pipeline.filter import keep_for_enrichment
from zaheendata.pipeline.match import attach_licenses
from zaheendata.pipeline.score import rank_leads, score_lead
from zaheendata.sources import tdlr, tsbpe
from zaheendata.sources.census_cbp import establishment_counts
from zaheendata.sources.google_places import PlacesConfigError, discover_places
from zaheendata.subsectors import Subsector, get_subsector

TX_COUNTIES = {
    "austin": "TRAVIS",
    "dallas": "DALLAS",
    "houston": "HARRIS",
    "san antonio": "BEXAR",
}


def load_licenses(subsector: Subsector, city: str, *, tsbpe_file: Path | None, cache: JsonCache) -> list[dict]:
    records: list[dict] = []
    source = subsector.license_source
    county = TX_COUNTIES.get(city.lower())
    if "tdlr_ac_contractor" in source:
        rows = tdlr.fetch_licenses(
            license_filter=tdlr.LICENSE_FILTERS["tdlr_ac_contractor"],
            county=county,
            cache=cache,
        )
        records.extend(tdlr.iter_match_records(rows, "tdlr:a/c_contractor"))
    if "tdlr_electrical_contractor" in source:
        rows = tdlr.fetch_licenses(
            license_filter=tdlr.LICENSE_FILTERS["tdlr_electrical_contractor"],
            county=county,
            cache=cache,
        )
        records.extend(tdlr.iter_match_records(rows, "tdlr:electrical_contractor"))
    if "tdlr_cosmetology_establishment" in source:
        rows = tdlr.fetch_licenses(
            license_filter=tdlr.LICENSE_FILTERS["tdlr_cosmetology_establishment"],
            county=county,
            cache=cache,
        )
        records.extend(tdlr.iter_match_records(rows, "tdlr:cosmetology_establishment"))
    if "tsbpe_rmp" in source:
        try:
            rows = tsbpe.load_rmp_rows(path=tsbpe_file, cache=cache)
            records.extend(tsbpe.iter_match_records(rows, city=city))
        except FileNotFoundError as exc:
            print(f"warning: {exc}")
    return records


def run_pipeline(
    *,
    subsector_key: str,
    city: str,
    state: str,
    keep: int = 100,
    cache_dir: Path = Path("cache"),
    tsbpe_file: Path | None = None,
    crawl: bool = True,
    concurrency: int = 8,
    max_pages: int = 8,
) -> tuple[list[Lead], dict]:
    subsector = get_subsector(subsector_key)
    cache = JsonCache(cache_dir)
    summary: dict = {
        "subsector": subsector.name,
        "naics": subsector.naics,
        "city": city,
        "state": state,
        "fit_signal": subsector.fit_signal,
        "census_cbp": establishment_counts(naics=subsector.naics, city=city, state=state),
    }

    try:
        discovered = discover_places(
            subsector, city=city, state=state, cache=cache, max_pages=max_pages
        )
    except PlacesConfigError as exc:
        raise SystemExit(str(exc)) from exc

    summary["discovered"] = len(discovered)
    licenses = load_licenses(subsector, city, tsbpe_file=tsbpe_file, cache=cache)
    summary["licenses"] = len(licenses)
    attach_licenses(discovered, licenses)
    enrichable = keep_for_enrichment(discovered, subsector)
    summary["rejected"] = sum(1 for lead in discovered if lead.status == "rejected")
    summary["enrichable"] = len(enrichable)

    if crawl:
        asyncio.run(crawl_leads(enrichable, concurrency=concurrency))
        summary["crawled"] = sum(1 for lead in enrichable if lead.website_crawled)

    for lead in enrichable:
        score_lead(lead, subsector)
    ranked = rank_leads(discovered, keep=keep)
    summary["selected"] = sum(1 for lead in ranked if lead.status == "selected")
    summary["with_website"] = sum(1 for lead in ranked if lead.website)
    summary["with_email"] = sum(1 for lead in ranked if lead.owner_email)
    return ranked, summary
