from __future__ import annotations

import signal
from pathlib import Path

from zaheendata.checkpoint import HarvestState, ProgressLog, StopFlag
from zaheendata.geo import parquet_stem, state_from_geo
from zaheendata.models import Lead
from zaheendata.pipeline.build import persist_leads, tool_histogram
from zaheendata.pipeline.download import download_licenses_for, download_overture
from zaheendata.pipeline.enrich import apply_stored_technologies, enrich_live
from zaheendata.pipeline.export import write_csv
from zaheendata.pipeline.filter import keep_for_enrichment
from zaheendata.pipeline.ingest import (
    attach_warehouse_overlays,
    dedupe_leads,
    hydrate_from_warehouse,
    ingest_license_json,
    ingest_overture_parquet,
    places_to_leads,
)
from zaheendata.pipeline.score import rank_leads, score_lead
from zaheendata.sources.licenses_catalog import soda_source_keys
from zaheendata.sources.overture import parquet_file_ready, parquet_row_count

# Statewide Texas across 17 trades is tens of thousands of places.
# A leftover `--limit` smoke parquet is a few rows and must not be reused.
MIN_FULL_PLACES = 100
from zaheendata.subsectors import SUBSECTORS, get_subsector
from zaheendata.warehouse import reset_warehouse, warehouse_stats

PRIORITY_SUBSECTORS = (
    "plumbing_hvac",
    "electrical",
    "beauty_salons",
    "nail_salons",
)


def ordered_subsectors(keys: list[str] | None = None) -> list[str]:
    wanted = list(keys) if keys else list(SUBSECTORS)
    unknown = [key for key in wanted if key not in SUBSECTORS]
    if unknown:
        raise KeyError(f"Unknown subsector(s): {', '.join(unknown)}")
    head = [key for key in PRIORITY_SUBSECTORS if key in wanted]
    tail = [key for key in wanted if key not in head]
    return head + tail


class HarvestStopped(Exception):
    def __init__(self, summary: dict) -> None:
        super().__init__("harvest stopped; rerun the same command to resume")
        self.summary = summary


def _raise_if_stopped(stop: StopFlag, state: HarvestState, summary: dict) -> None:
    if stop.requested:
        state.data["stopped"] = True
        state.data["phase"] = "paused"
        state.save()
        raise HarvestStopped(summary)


def harvest(
    *,
    geo: str = "TX",
    subsector_keys: list[str] | None = None,
    warehouse: Path = Path("data/warehouse.duckdb"),
    raw_dir: Path = Path("data/raw"),
    out_dir: Path = Path("data/exports"),
    state_path: Path | None = None,
    log_path: Path | None = None,
    keep: int = 100,
    limit: int | None = None,
    crawl: str = "live",
    concurrency: int = 4,
    fresh: bool = False,
    refetch: bool = False,
    min_full_rows: int = MIN_FULL_PLACES,
) -> dict:
    keys = ordered_subsectors(subsector_keys)
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_path or raw_dir.parent / "harvest_state.json"
    log_path = log_path or raw_dir.parent / "harvest.log"
    progress = ProgressLog(log_path)

    if fresh:
        reset_warehouse(warehouse)
        if state_path.exists():
            state_path.unlink()
        progress.emit(
            f"fresh start: deleted {warehouse} (downloaded files in {raw_dir} are kept unless --refetch)"
        )

    state = HarvestState.load(state_path)
    state.data["geo"] = geo
    state.data["stopped"] = False
    state.data.setdefault("subsectors", {})
    state.save()

    stop = StopFlag(on_stop=state.save)
    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM) if hasattr(signal, "SIGTERM") else None
    stop.install()

    modes = {part.strip() for part in crawl.split(",") if part.strip()}
    summary: dict = {
        "geo": geo,
        "subsectors": keys,
        "warehouse": str(warehouse),
        "raw_dir": str(raw_dir),
        "out_dir": str(out_dir),
        "stopped": False,
        "resume_command": f"zaheendata harvest --geo {geo}",
        "trades": {},
    }

    try:
        _download_places(
            geo,
            raw_dir,
            refetch=refetch,
            min_full_rows=min_full_rows,
            state=state,
            progress=progress,
            stop=stop,
            summary=summary,
        )
        _download_licenses(geo, raw_dir, refetch, state, progress, stop, summary)
        _ingest(geo, raw_dir, warehouse, state, progress, stop, summary)
        for key in keys:
            _raise_if_stopped(stop, state, summary)
            summary["trades"][key] = _harvest_subsector(
                key=key,
                geo=geo,
                warehouse=warehouse,
                out_dir=out_dir,
                keep=keep,
                limit=limit,
                modes=modes,
                concurrency=concurrency,
                state=state,
                progress=progress,
                stop=stop,
            )
            if stop.requested:
                break
        _raise_if_stopped(stop, state, summary)
        state.set_phase("complete")
        progress.emit("harvest complete")
    except HarvestStopped as exc:
        summary.update(exc.summary)
        summary["stopped"] = True
        progress.emit("paused. rerun the same command (without --fresh) to continue.")
        return summary
    finally:
        signal.signal(signal.SIGINT, prev_int)
        if prev_term is not None:
            signal.signal(signal.SIGTERM, prev_term)

    summary["warehouse_stats"] = warehouse_stats(warehouse)
    summary["state"] = str(state_path)
    summary["log"] = str(log_path)
    return summary


def _download_places(
    geo: str,
    raw_dir: Path,
    *,
    refetch: bool,
    min_full_rows: int,
    state: HarvestState,
    progress: ProgressLog,
    stop: StopFlag,
    summary: dict,
) -> Path:
    dest = raw_dir / f"{parquet_stem(geo, None)}.parquet"
    info = state.data.setdefault("overture", {})
    rows = parquet_row_count(dest)
    sample_leftover = parquet_file_ready(dest) and rows < min_full_rows
    if sample_leftover:
        progress.emit(
            f"overture: {dest} only has {rows} places (smoke-test sample). "
            "Downloading the full statewide file."
        )
    if not refetch and parquet_file_ready(dest) and not sample_leftover:
        info.update({"status": "ready", "path": str(dest), "bytes": dest.stat().st_size, "places": rows})
        state.save()
        progress.emit(f"overture: reusing {dest} ({rows} places, {dest.stat().st_size} bytes)")
        summary["overture"] = str(dest)
        return dest
    _raise_if_stopped(stop, state, summary)
    state.set_phase("download_places")
    progress.emit(f"overture: downloading all 17 trades for {geo} → {dest}")
    download_overture(geo=geo, subsector_key=None, raw_dir=raw_dir, limit=None, out=dest, refetch=True)
    rows = parquet_row_count(dest)
    if rows <= 0:
        raise RuntimeError(f"Overture download did not produce a readable parquet at {dest}")
    info.update({"status": "ready", "path": str(dest), "bytes": dest.stat().st_size, "places": rows})
    state.save()
    progress.emit(f"overture: saved {dest} ({rows} places, {dest.stat().st_size} bytes)")
    summary["overture"] = str(dest)
    return dest


def _download_licenses(
    geo: str,
    raw_dir: Path,
    refetch: bool,
    state: HarvestState,
    progress: ProgressLog,
    stop: StopFlag,
    summary: dict,
) -> list[Path]:
    _raise_if_stopped(stop, state, summary)
    state.set_phase("download_licenses")
    paths = download_licenses_for(geo=geo, subsector_key=None, raw_dir=raw_dir, refetch=refetch)
    recorded = state.data.setdefault("licenses", {})
    for path in paths:
        recorded[path.stem] = {"status": "ready", "path": str(path), "bytes": path.stat().st_size}
        progress.emit(f"licenses: {path.name} ({path.stat().st_size} bytes)")
    state.save()
    summary["license_files"] = [str(path) for path in paths]
    return paths


def _ingest(
    geo: str,
    raw_dir: Path,
    warehouse: Path,
    state: HarvestState,
    progress: ProgressLog,
    stop: StopFlag,
    summary: dict,
) -> None:
    _raise_if_stopped(stop, state, summary)
    parquet = raw_dir / f"{parquet_stem(geo, None)}.parquet"
    ingest_info = state.data.setdefault("ingest", {})
    stats = warehouse_stats(warehouse)
    if ingest_info.get("places") and stats.get("places"):
        progress.emit(f"ingest: warehouse already has {stats['places']} places")
    else:
        state.set_phase("ingest_places")
        progress.emit(f"ingest: loading {parquet} into {warehouse}")
        count = ingest_overture_parquet(parquet, warehouse)
        ingest_info["places"] = count
        state.save()
        progress.emit(f"ingest: {count} places")
    license_count = 0
    for path in sorted(raw_dir.glob("tx_*.json")):
        license_count += ingest_license_json(path, warehouse, state=state_from_geo(geo) or "")
        progress.emit(f"ingest: licenses from {path.name}")
    ingest_info["licenses"] = license_count
    state.save()
    summary["ingested_places"] = ingest_info.get("places")
    summary["ingested_licenses"] = license_count


def _harvest_subsector(
    *,
    key: str,
    geo: str,
    warehouse: Path,
    out_dir: Path,
    keep: int,
    limit: int | None,
    modes: set[str],
    concurrency: int,
    state: HarvestState,
    progress: ProgressLog,
    stop: StopFlag,
) -> dict:
    spec = get_subsector(key)
    row = state.subsector(key)
    if row.get("status") == "complete":
        progress.emit(f"{key}: already complete — skipping")
        return dict(row)

    state.set_phase(f"crawl:{key}")
    row["status"] = "running"
    state.save()
    progress.emit(f"{key}: building lead list")

    leads = dedupe_leads(places_to_leads(warehouse, subsector=key, own_website_only=True))
    hydrate_from_warehouse(leads, warehouse)
    apply_stored_technologies(leads, warehouse)
    allowed = soda_source_keys(state_from_geo(geo), key)
    attach_warehouse_overlays(leads, warehouse, allowed_sources=allowed)
    enrichable = keep_for_enrichment(leads, spec, require_reviews=False)
    if limit:
        enrichable = enrichable[:limit]
    row["discovered"] = len(leads)
    row["enrichable"] = len(enrichable)
    state.save()
    progress.emit(f"{key}: {len(leads)} discovered, {len(enrichable)} enrichable")

    crawled_before = sum(1 for lead in enrichable if lead.website_crawled)
    out = out_dir / f"{geo}_{key}.csv"

    def on_progress(lead: Lead) -> None:
        score_lead(lead, spec)
        row["crawled"] = sum(1 for item in enrichable if item.website_crawled)
        row["failed"] = sum(
            1
            for item in enrichable
            if item.extra.get("crawl_attempted") and not item.website_crawled
        )
        row["last_domain"] = lead.domain
        state.save()
        done = row["crawled"] + row["failed"]
        total = len(enrichable)
        flag = "ok" if lead.website_crawled else "fail"
        tools = lead.tech_stack or "-"
        progress.emit(
            f"{key}: {done}/{total} {flag} {lead.domain or lead.name} tools={tools}"
        )
        if done == 1 or done % 25 == 0:
            persist_leads(enrichable, warehouse)
            write_csv(enrichable, out)

    if "live" in modes:
        enrich_live(
            enrichable,
            warehouse=warehouse,
            concurrency=concurrency,
            only_missing=True,
            should_stop=stop,
            on_progress=on_progress,
        )
    apply_stored_technologies(enrichable, warehouse)

    for lead in enrichable:
        score_lead(lead, spec)
    ranked = rank_leads(leads, keep=keep)
    persist_leads(ranked, warehouse)
    write_csv(ranked, out)

    row.update(
        {
            "status": "paused" if stop.requested else "complete",
            "crawled": sum(1 for lead in enrichable if lead.website_crawled),
            "failed": sum(
                1
                for lead in enrichable
                if lead.extra.get("crawl_attempted") and not lead.website_crawled
            ),
            "selected": sum(1 for lead in ranked if lead.status == "selected"),
            "with_tools": sum(1 for lead in enrichable if lead.tech_stack),
            "with_license": sum(1 for lead in ranked if lead.license_number),
            "out": str(out),
            "tools": tool_histogram(enrichable),
            "resumed_from": crawled_before,
        }
    )
    state.save()
    progress.emit(
        f"{key}: {row['status']} crawled={row['crawled']} selected={row['selected']} → {out}"
    )
    return dict(row)
