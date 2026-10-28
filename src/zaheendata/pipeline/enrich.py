from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import httpx

from zaheendata.cache import JsonCache
from zaheendata.fingerprints import detect_tools, summarize_tools
from zaheendata.models import Lead
from zaheendata.pipeline.crawl import apply_signals, crawl_leads, extract_signals
from zaheendata.sources.commoncrawl import latest_crawl_id, snapshot_domain
from zaheendata.warehouse import connect


def _record_snapshot(con, lead: Lead, source: str, html: str, cache_path: str) -> None:
    if not lead.domain:
        return
    con.execute(
        "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            lead.domain,
            lead.website,
            source,
            datetime.now(timezone.utc),
            200 if html else 0,
            len(html or ""),
            cache_path,
        ],
    )
    if not html:
        return
    con.execute("DELETE FROM technologies WHERE domain = ? AND source = ?", [lead.domain, source])
    matches = detect_tools(html.lower())
    if matches:
        con.executemany(
            "INSERT INTO technologies VALUES (?, ?, ?, ?, ?)",
            [[lead.domain, row["name"], row["category"], row["evidence"], source] for row in matches],
        )


def enrich_commoncrawl(
    leads: list[Lead],
    *,
    warehouse: Path | None = None,
    cache_dir: Path = Path("cache"),
    limit: int | None = None,
) -> list[Lead]:
    cache = JsonCache(cache_dir)
    client = httpx.Client(timeout=40.0, follow_redirects=True)
    targets = [lead for lead in leads if lead.website and lead.domain][: limit or len(leads)]
    con = connect(warehouse)
    try:
        crawl_id = latest_crawl_id(client)
        for lead in targets:
            snap = snapshot_domain(
                lead.domain,
                crawl_id=crawl_id,
                cache=cache,
                html_dir=cache_dir / "html",
                client=client,
            )
            html = snap.get("html") or ""
            if not html:
                continue
            signals = extract_signals(html, lead.website)
            apply_signals(lead, signals)
            lead.extra.pop("_html", None)
            lead.merge_source("commoncrawl")
            _record_snapshot(con, lead, "commoncrawl", html, snap.get("cache_path") or "")
    finally:
        client.close()
        con.close()
    return leads


def enrich_live(
    leads: list[Lead],
    *,
    warehouse: Path | None = None,
    concurrency: int = 8,
    only_missing: bool = True,
) -> list[Lead]:
    targets = [
        lead
        for lead in leads
        if lead.website and (not only_missing or not lead.website_crawled)
    ]
    con = connect(warehouse)

    def on_html(lead: Lead, html: str) -> None:
        lead.merge_source("live_crawl")
        _record_snapshot(con, lead, "live_crawl", html, "")

    try:
        asyncio.run(crawl_leads(targets, concurrency=concurrency, on_html=on_html))
    finally:
        con.close()
    return leads


def apply_stored_technologies(leads: list[Lead], warehouse: Path | None = None) -> list[Lead]:
    con = connect(warehouse)
    rows = con.execute("SELECT domain, tool, category FROM technologies").fetchall()
    con.close()
    by_domain: dict[str, list[dict[str, str]]] = {}
    for domain, tool, category in rows:
        by_domain.setdefault(domain, []).append({"name": tool, "category": category, "evidence": ""})
    for lead in leads:
        matches = by_domain.get(lead.domain or "", [])
        if not matches:
            continue
        summary = summarize_tools(matches)
        existing = {part for part in (lead.tech_stack or "").split(",") if part}
        combined = list(dict.fromkeys([*existing, *[row["name"] for row in matches]]))
        lead.tech_stack = ",".join(combined) if combined else summary["tech_stack"]
        cats = {part for part in (lead.tool_categories or "").split(",") if part}
        cats.update(summary["tool_categories"].split(",") if summary["tool_categories"] else [])
        lead.tool_categories = ",".join(sorted(cats))
        lead.booking_provider = lead.booking_provider or summary["booking_provider"]
        lead.store_platform = lead.store_platform or summary["store_platform"]
        if summary["booking_provider"]:
            lead.online_booking = True
        if summary["store_platform"]:
            lead.online_store = True
    return leads
