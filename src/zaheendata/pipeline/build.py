from __future__ import annotations

from pathlib import Path

from zaheendata.geo import parquet_stem, state_from_geo
from zaheendata.models import Lead
from zaheendata.pipeline.download import download_licenses_for, download_overture, download_ppp_for
from zaheendata.pipeline.enrich import apply_stored_technologies, enrich_commoncrawl, enrich_live
from zaheendata.pipeline.export import write_csv
from zaheendata.pipeline.filter import keep_for_enrichment
from zaheendata.pipeline.ingest import attach_warehouse_overlays, ingest_license_json, ingest_overture_parquet, ingest_ppp_file, places_to_leads
from zaheendata.pipeline.score import rank_leads, score_lead
from zaheendata.subsectors import get_subsector
from zaheendata.warehouse import connect, warehouse_stats

LEAD_COLUMNS = (
    "business_id",
    "name",
    "phone",
    "website",
    "website_crawled",
    "address",
    "city",
    "state",
    "postal_code",
    "owner_name",
    "owner_email",
    "owner_phone",
    "employee_count",
    "naics",
    "score_total",
    "rank",
    "status",
    "sources",
    "lat",
    "lng",
    "domain",
    "google_place_id",
    "categories",
    "owner_title",
    "owner_linkedin",
    "owner_source",
    "owner_confidence",
    "email_is_role_account",
    "suppressed",
    "suppression_reasons",
    "rejection_reason",
    "license_number",
    "license_status",
    "online_booking",
    "booking_provider",
    "quote_form",
    "says_call_for_quote",
    "emergency_service",
    "maintenance_plan",
    "recurring_service",
    "free_estimate",
    "online_store",
    "store_platform",
    "sells_gift_cards",
    "hosts_events",
    "instagram",
    "tech_stack",
    "tool_categories",
    "pain_mentions",
    "pain_examples",
    "google_rating",
    "google_reviews",
    "recent_reviews_90d",
    "last_review_at",
    "busy_and_growing",
    "admin_pain",
    "multi_tool",
    "owner_reachable",
    "subsector_signal",
    "score_evidence",
    "subsector",
)
LEAD_INSERT_SQL = (
    "INSERT OR REPLACE INTO leads ("
    + ", ".join(LEAD_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in LEAD_COLUMNS)
    + ")"
)


def _lead_values(lead: Lead) -> list:
    row = lead.to_export_row()
    return [
        lead.business_id or lead.name,
        row["name"],
        row["phone"],
        row["website"],
        bool(lead.website_crawled),
        row["address"],
        row["city"],
        row["state"],
        row["postal_code"],
        row["owner_name"],
        row["owner_email"],
        row["owner_phone"],
        row["employee_count"],
        row["naics"],
        lead.score_total if lead.score_total != "" else None,
        lead.rank if lead.rank != "" else None,
        row["status"],
        row["sources"],
        lead.lat if lead.lat != "" else None,
        lead.lng if lead.lng != "" else None,
        row["domain"],
        row["google_place_id"],
        row["categories"],
        row["owner_title"],
        row["owner_linkedin"],
        row["owner_source"],
        lead.owner_confidence if lead.owner_confidence != "" else None,
        bool(lead.email_is_role_account),
        bool(lead.suppressed),
        row["suppression_reasons"],
        row["rejection_reason"],
        row["license_number"],
        row["license_status"],
        bool(lead.online_booking) if lead.online_booking != "" else None,
        row["booking_provider"],
        bool(lead.quote_form) if lead.quote_form != "" else None,
        bool(lead.says_call_for_quote) if lead.says_call_for_quote != "" else None,
        bool(lead.emergency_service) if lead.emergency_service != "" else None,
        bool(lead.maintenance_plan) if lead.maintenance_plan != "" else None,
        bool(lead.recurring_service) if lead.recurring_service != "" else None,
        bool(lead.free_estimate) if lead.free_estimate != "" else None,
        bool(lead.online_store) if lead.online_store != "" else None,
        row["store_platform"],
        bool(lead.sells_gift_cards) if lead.sells_gift_cards != "" else None,
        bool(lead.hosts_events) if lead.hosts_events != "" else None,
        row["instagram"],
        row["tech_stack"],
        row["tool_categories"],
        row["pain_mentions"],
        row["pain_examples"],
        lead.google_rating if lead.google_rating != "" else None,
        lead.google_reviews if lead.google_reviews != "" else None,
        lead.recent_reviews_90d if lead.recent_reviews_90d != "" else None,
        row["last_review_at"],
        lead.busy_and_growing if lead.busy_and_growing != "" else None,
        lead.admin_pain if lead.admin_pain != "" else None,
        lead.multi_tool if lead.multi_tool != "" else None,
        lead.owner_reachable if lead.owner_reachable != "" else None,
        lead.subsector_signal if lead.subsector_signal != "" else None,
        row["score_evidence"],
        lead.extra.get("subsector") or "",
    ]


def persist_leads(leads: list[Lead], warehouse: Path | None = None) -> None:
    if not leads:
        return
    rows = [_lead_values(lead) for lead in leads]
    if any(len(row) != len(LEAD_COLUMNS) for row in rows):
        raise ValueError(
            f"lead row width {len(rows[0])} does not match {len(LEAD_COLUMNS)} warehouse columns"
        )
    con = connect(warehouse)
    con.executemany(LEAD_INSERT_SQL, rows)
    con.close()


def tool_histogram(leads: list[Lead]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for lead in leads:
        for tool in (lead.tech_stack or "").split(","):
            tool = tool.strip()
            if tool:
                counts[tool] = counts.get(tool, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))


def build_national(
    *,
    geo: str,
    subsector_key: str,
    warehouse: Path = Path("data/warehouse.duckdb"),
    raw_dir: Path = Path("data/raw"),
    out: Path | None = None,
    keep: int = 100,
    limit: int | None = None,
    crawl: str = "live",
    skip_download: bool = False,
    concurrency: int = 8,
    licenses: bool = False,
    ppp: bool = False,
) -> tuple[list[Lead], dict]:
    subsector = get_subsector(subsector_key)
    raw_dir.mkdir(parents=True, exist_ok=True)
    parquet = raw_dir / f"{parquet_stem(geo, subsector_key)}.parquet"
    modes = {part.strip() for part in crawl.split(",") if part.strip()}
    summary: dict = {
        "geo": geo,
        "subsector": subsector.name,
        "naics": subsector.naics,
        "state": state_from_geo(geo),
        "crawl": sorted(modes),
    }

    if not skip_download:
        download_overture(geo=geo, subsector_key=subsector_key, raw_dir=raw_dir, limit=limit, out=parquet)
        summary["downloaded"] = str(parquet)

    if licenses:
        paths = download_licenses_for(geo=geo, subsector_key=subsector_key, raw_dir=raw_dir)
        summary["license_files"] = [str(path) for path in paths]
        summary["licenses_ingested"] = 0
        for path in paths:
            summary["licenses_ingested"] += ingest_license_json(path, warehouse, state=state_from_geo(geo) or "")
    if ppp:
        ppp_path = download_ppp_for(subsector_key=subsector_key, geo=geo, raw_dir=raw_dir)
        summary["ppp_file"] = str(ppp_path)
        ingest_ppp_file(ppp_path, warehouse)

    if parquet.exists():
        summary["ingested"] = ingest_overture_parquet(parquet, warehouse)

    leads = places_to_leads(warehouse, subsector=subsector_key, own_website_only=True)
    attach_warehouse_overlays(leads, warehouse)
    enrichable = keep_for_enrichment(leads, subsector, require_reviews=False)
    summary["discovered"] = len(leads)
    summary["enrichable"] = len(enrichable)

    crawl_targets = enrichable[:limit] if limit else enrichable
    if "commoncrawl" in modes:
        enrich_commoncrawl(crawl_targets, warehouse=warehouse, limit=limit)
    if "live" in modes:
        enrich_live(crawl_targets, warehouse=warehouse, concurrency=concurrency, only_missing=True)
    apply_stored_technologies(enrichable, warehouse)

    for lead in enrichable:
        score_lead(lead, subsector)
    ranked = rank_leads(leads, keep=keep)
    persist_leads(ranked, warehouse)
    if out:
        write_csv(ranked, out)
        summary["out"] = str(out)
    summary["selected"] = sum(1 for lead in ranked if lead.status == "selected")
    summary["with_website"] = sum(1 for lead in ranked if lead.website)
    summary["with_email"] = sum(1 for lead in ranked if lead.owner_email)
    summary["with_license"] = sum(1 for lead in ranked if lead.license_number)
    summary["with_person_owner"] = sum(1 for lead in ranked if lead.owner_name)
    summary["with_tools"] = sum(1 for lead in ranked if lead.tech_stack)
    summary["crawled"] = sum(1 for lead in enrichable if lead.website_crawled)
    summary["tools"] = tool_histogram(enrichable)
    summary["warehouse"] = warehouse_stats(warehouse)
    return ranked, summary
