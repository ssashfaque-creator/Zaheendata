from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from zaheendata.geo import resolve_bbox
from zaheendata.pipeline.build import build_national, persist_leads
from zaheendata.pipeline.diagnose import diagnose_csv, render_report
from zaheendata.pipeline.download import download_licenses_for, download_overture, download_ppp_for
from zaheendata.pipeline.enrich import apply_stored_technologies, enrich_commoncrawl, enrich_live
from zaheendata.pipeline.export import write_csv
from zaheendata.pipeline.filter import keep_for_enrichment
from zaheendata.pipeline.ingest import (
    attach_warehouse_overlays,
    ingest_license_json,
    ingest_overture_parquet,
    ingest_ppp_file,
    places_to_leads,
)
from zaheendata.pipeline.run import run_pipeline
from zaheendata.pipeline.score import rank_leads, score_lead
from zaheendata.sources.licenses_catalog import FEEDS, list_feeds
from zaheendata.sources.overture import OVERTURE_RELEASE, probe_places
from zaheendata.subsectors import SUBSECTORS, get_subsector
from zaheendata.warehouse import reset_warehouse, warehouse_stats


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _default_warehouse() -> Path:
    return Path(os.environ.get("ZAHEENDATA_WAREHOUSE", "data/warehouse.duckdb"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zaheendata",
        description="National download-first warehouse for qualified local-service leads.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Download free sources, warehouse, enrich tools, score, export.")
    build.add_argument("--geo", default="Austin,TX", help="US, state code (TX), or city,state (Austin,TX).")
    build.add_argument("--subsector", required=True, choices=sorted(SUBSECTORS))
    build.add_argument("--keep", type=int, default=100)
    build.add_argument("--out", type=Path, default=Path("data/leads.csv"))
    build.add_argument("--warehouse", type=Path, default=None)
    build.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    build.add_argument("--limit", type=int, help="Cap download and crawl size (good for a metro smoke).")
    build.add_argument("--crawl", default="live", help="none, live, commoncrawl, or live,commoncrawl.")
    build.add_argument("--skip-download", action="store_true")
    build.add_argument("--concurrency", type=int, default=8)
    build.add_argument("--licenses", action="store_true", help="Also download matching SODA license feeds.")
    build.add_argument("--ppp", action="store_true", help="Overlay SBA PPP jobs_reported (size proxy, not discovery).")

    harvest_cmd = sub.add_parser(
        "harvest",
        help="Resumable statewide pull for every trade. Ctrl+C saves; rerun the same command to continue.",
    )
    harvest_cmd.add_argument("--geo", default="TX", help="State code (TX) or US. Default TX.")
    harvest_cmd.add_argument(
        "--subsector",
        nargs="*",
        choices=sorted(SUBSECTORS),
        help="Default: all 17 trades. Pass one or more to restrict.",
    )
    harvest_cmd.add_argument(
        "--fresh",
        action="store_true",
        help="Delete the warehouse and harvest_state.json first. Keeps downloaded parquets.",
    )
    harvest_cmd.add_argument(
        "--refetch",
        action="store_true",
        help="Re-download Overture and license files even if they already exist.",
    )
    harvest_cmd.add_argument("--warehouse", type=Path, default=None)
    harvest_cmd.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    harvest_cmd.add_argument("--out-dir", type=Path, default=Path("data/exports"))
    harvest_cmd.add_argument("--keep", type=int, default=100)
    harvest_cmd.add_argument("--limit", type=int, help="Cap enrichable sites per trade (smoke test).")
    harvest_cmd.add_argument("--crawl", default="live", help="live or none.")
    harvest_cmd.add_argument("--concurrency", type=int, default=4)

    reset = sub.add_parser("reset", help="Delete the DuckDB warehouse. Downloaded files are kept.")
    reset.add_argument("--warehouse", type=Path, default=None)
    reset.add_argument("--state", type=Path, default=Path("data/harvest_state.json"))

    download = sub.add_parser("download", help="Bulk-download a free source before scoring.")
    download.add_argument("source", choices=("places", "ppp", "licenses"))
    download.add_argument("--geo", default="Austin,TX")
    download.add_argument("--subsector", choices=sorted(SUBSECTORS))
    download.add_argument("--out", type=Path)
    download.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    download.add_argument("--limit", type=int)
    download.add_argument("--feed", help="License catalog key, e.g. tx_tdlr_ac.")
    download.add_argument("--naics")
    download.add_argument("--state")

    ingest = sub.add_parser("ingest", help="Load a downloaded file into DuckDB.")
    ingest.add_argument("--places", type=Path)
    ingest.add_argument("--licenses", type=Path)
    ingest.add_argument("--ppp", type=Path)
    ingest.add_argument("--state", default="")
    ingest.add_argument("--warehouse", type=Path, default=None)

    enrich = sub.add_parser("enrich", help="Detect tools from Common Crawl and/or live site HTML.")
    enrich.add_argument("--subsector", required=True, choices=sorted(SUBSECTORS))
    enrich.add_argument("--mode", default="live", help="live, commoncrawl, or live,commoncrawl.")
    enrich.add_argument("--warehouse", type=Path, default=None)
    enrich.add_argument("--limit", type=int)
    enrich.add_argument("--concurrency", type=int, default=8)

    score = sub.add_parser("score", help="Score warehouse places and write the ranked CSV.")
    score.add_argument("--subsector", required=True, choices=sorted(SUBSECTORS))
    score.add_argument("--keep", type=int, default=100)
    score.add_argument("--out", type=Path, default=Path("data/leads.csv"))
    score.add_argument("--warehouse", type=Path, default=None)

    export = sub.add_parser("export", help="Export scored leads from the warehouse.")
    export.add_argument("--out", type=Path, default=Path("data/leads.csv"))
    export.add_argument("--warehouse", type=Path, default=None)
    export.add_argument("--subsector", choices=sorted(SUBSECTORS))
    export.add_argument("--status", default="selected")

    stats = sub.add_parser("stats", help="Print DuckDB warehouse counts and detected tools.")
    stats.add_argument("--warehouse", type=Path, default=None)

    doctor = sub.add_parser("doctor", help="Check DuckDB + public Overture S3 access.")
    doctor.add_argument("--geo", default="Austin,TX")

    licenses = sub.add_parser("licenses", help="List national license feeds (bulk first, manual noted).")
    licenses.add_argument("--state")
    licenses.add_argument("--subsector", choices=sorted(SUBSECTORS))

    run = sub.add_parser("run", help="Optional Google Places metro overlay (paid/quota later).")
    run.add_argument("--subsector", required=True, choices=sorted(SUBSECTORS))
    run.add_argument("--city", default="Austin")
    run.add_argument("--state", default="TX")
    run.add_argument("--keep", type=int, default=100)
    run.add_argument("--out", type=Path, default=Path("data/leads.csv"))
    run.add_argument("--cache-dir", type=Path, default=Path("cache"))
    run.add_argument("--tsbpe-file", type=Path)
    run.add_argument("--no-crawl", action="store_true")
    run.add_argument("--concurrency", type=int, default=8)
    run.add_argument("--max-pages", type=int, default=8)

    diag = sub.add_parser("diagnose", help="Explain coverage gaps in an existing CSV export.")
    diag.add_argument("csv_path", type=Path)

    listing = sub.add_parser("subsectors", help="List supported sub-sectors.")
    listing.set_defaults(command="subsectors")
    return parser


def _warehouse(args: argparse.Namespace) -> Path:
    return getattr(args, "warehouse", None) or _default_warehouse()


def _cmd_download(args: argparse.Namespace) -> int:
    if args.source == "places":
        dest = download_overture(
            geo=args.geo,
            subsector_key=args.subsector,
            raw_dir=args.raw_dir,
            limit=args.limit,
            out=args.out,
        )
        west, south, east, north = resolve_bbox(args.geo)
        print(json.dumps({
            "source": "overture_places",
            "release": OVERTURE_RELEASE,
            "geo": args.geo,
            "bbox": [west, south, east, north],
            "subsector": args.subsector or "all",
            "out": str(dest),
        }, indent=2))
        return 0
    if args.source == "ppp":
        if args.subsector:
            dest = download_ppp_for(
                subsector_key=args.subsector,
                geo=args.state or args.geo,
                raw_dir=args.raw_dir,
            )
        else:
            from zaheendata.sources.ppp import download_ppp

            dest = download_ppp(
                out=args.out or args.raw_dir / "ppp.parquet",
                naics=args.naics,
                state=args.state,
            )
        print(json.dumps({"source": "sba_ppp", "out": str(dest)}, indent=2))
        return 0
    if args.feed:
        from zaheendata.sources.licenses_catalog import download_soda_feed

        feed = next((item for item in FEEDS if item.key == args.feed), None)
        if feed is None:
            print(f"unknown license feed {args.feed}")
            return 2
        if feed.kind != "soda":
            print(json.dumps({"feed": feed.key, "kind": feed.kind, "url": feed.url, "notes": feed.notes}, indent=2))
            return 2
        dest = args.out or args.raw_dir / f"{feed.key}.json"
        download_soda_feed(feed, dest)
        print(json.dumps({"source": feed.key, "out": str(dest)}, indent=2))
        return 0
    if not args.subsector:
        print("download licenses needs --subsector or --feed")
        return 2
    paths = download_licenses_for(geo=args.geo, subsector_key=args.subsector, raw_dir=args.raw_dir)
    print(json.dumps({"source": "licenses", "files": [str(path) for path in paths]}, indent=2))
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    warehouse = _warehouse(args)
    summary = {"warehouse": str(warehouse)}
    if args.places:
        summary["places"] = ingest_overture_parquet(args.places, warehouse)
    if args.licenses:
        summary["licenses"] = ingest_license_json(args.licenses, warehouse, state=args.state)
    if args.ppp:
        summary["ppp"] = ingest_ppp_file(args.ppp, warehouse)
    if len(summary) == 1:
        print("pass --places, --licenses, and/or --ppp")
        return 2
    print(json.dumps(summary, indent=2))
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    warehouse = _warehouse(args)
    subsector = get_subsector(args.subsector)
    leads = places_to_leads(warehouse, subsector=args.subsector, own_website_only=True, limit=args.limit)
    attach_warehouse_overlays(leads, warehouse)
    enrichable = keep_for_enrichment(leads, subsector, require_reviews=False)
    modes = {part.strip() for part in args.mode.split(",") if part.strip()}
    if "commoncrawl" in modes:
        enrich_commoncrawl(enrichable, warehouse=warehouse, limit=args.limit)
    if "live" in modes:
        enrich_live(enrichable, warehouse=warehouse, concurrency=args.concurrency, only_missing=True)
    apply_stored_technologies(enrichable, warehouse)
    persist_leads(enrichable, warehouse)
    print(json.dumps({
        "enrichable": len(enrichable),
        "crawled": sum(1 for lead in enrichable if lead.website_crawled),
        "with_tools": sum(1 for lead in enrichable if lead.tech_stack),
        "warehouse": str(warehouse),
    }, indent=2))
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    warehouse = _warehouse(args)
    subsector = get_subsector(args.subsector)
    leads = places_to_leads(warehouse, subsector=args.subsector, own_website_only=True)
    attach_warehouse_overlays(leads, warehouse)
    apply_stored_technologies(leads, warehouse)
    enrichable = keep_for_enrichment(leads, subsector, require_reviews=False)
    for lead in enrichable:
        score_lead(lead, subsector)
    ranked = rank_leads(leads, keep=args.keep)
    persist_leads(ranked, warehouse)
    write_csv(ranked, args.out)
    print(json.dumps({
        "discovered": len(leads),
        "selected": sum(1 for lead in ranked if lead.status == "selected"),
        "with_tools": sum(1 for lead in ranked if lead.tech_stack),
        "out": str(args.out),
    }, indent=2))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from zaheendata.models import empty_lead
    from zaheendata.warehouse import connect

    warehouse = _warehouse(args)
    con = connect(warehouse)
    where = "WHERE 1=1"
    params: list = []
    if args.subsector:
        where += " AND subsector = ?"
        params.append(args.subsector)
    if args.status:
        where += " AND status = ?"
        params.append(args.status)
    rows = con.execute(f"SELECT * FROM leads {where}", params).fetchall()
    columns = [item[0] for item in con.description]
    con.close()
    leads = []
    for row in rows:
        data = dict(zip(columns, row))
        extra = {"subsector": data.pop("subsector", "")}
        lead = empty_lead(**{key: (value if value is not None else "") for key, value in data.items()})
        lead.extra.update(extra)
        leads.append(lead)
    write_csv(leads, args.out)
    print(json.dumps({"rows": len(leads), "out": str(args.out)}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(".env"))
    args = build_parser().parse_args(argv)
    if args.command == "subsectors":
        for key, spec in SUBSECTORS.items():
            area = "state/national" if spec.wide_area else "metro"
            cats = ",".join(spec.overture_categories)
            print(f"{key:20} NAICS {spec.naics}  {spec.name}  [{area}]")
            print(f"{'':20} overture: {cats}")
            print(f"{'':20} license:  {spec.license_source}")
            print(f"{'':20} signal:   {spec.fit_signal}")
        return 0
    if args.command == "diagnose":
        print(render_report(diagnose_csv(args.csv_path)))
        return 0
    if args.command == "licenses":
        feeds = list_feeds(state=args.state, subsector=args.subsector)
        for feed in feeds:
            print(f"{feed.key:22} {','.join(feed.states):8} {feed.kind:8} {feed.url}")
            print(f"{'':22} {feed.notes}")
        return 0
    if args.command == "reset":
        path = reset_warehouse(_warehouse(args))
        if args.state.exists():
            args.state.unlink()
        print(json.dumps({"reset": str(path), "state": str(args.state)}, indent=2))
        return 0
    if args.command == "harvest":
        from zaheendata.pipeline.harvest import harvest

        if args.warehouse is None:
            args.warehouse = _default_warehouse()
        summary = harvest(
            geo=args.geo,
            subsector_keys=args.subsector,
            warehouse=args.warehouse,
            raw_dir=args.raw_dir,
            out_dir=args.out_dir,
            keep=args.keep,
            limit=args.limit,
            crawl=args.crawl,
            concurrency=args.concurrency,
            fresh=args.fresh,
            refetch=args.refetch,
        )
        print(json.dumps(summary, indent=2, default=str))
        return 0
    if args.command == "stats":
        print(json.dumps(warehouse_stats(_warehouse(args)), indent=2, default=str))
        return 0
    if args.command == "doctor":
        try:
            result = probe_places(args.geo)
        except Exception as exc:  # noqa: BLE001 - doctor should always print the failure
            print(json.dumps({"ok": False, "error": str(exc), "release": OVERTURE_RELEASE}, indent=2))
            return 1
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "download":
        return _cmd_download(args)
    if args.command == "ingest":
        return _cmd_ingest(args)
    if args.command == "enrich":
        return _cmd_enrich(args)
    if args.command == "score":
        return _cmd_score(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "build":
        if args.warehouse is None:
            args.warehouse = _default_warehouse()
        leads, summary = build_national(
            geo=args.geo,
            subsector_key=args.subsector,
            warehouse=args.warehouse,
            raw_dir=args.raw_dir,
            out=args.out,
            keep=args.keep,
            limit=args.limit,
            crawl=args.crawl,
            skip_download=args.skip_download,
            concurrency=args.concurrency,
            licenses=args.licenses,
            ppp=args.ppp,
        )
        print(json.dumps(summary, indent=2, default=str))
        print(f"wrote {args.out} ({len(leads)} rows)")
        return 0

    leads, summary = run_pipeline(
        subsector_key=args.subsector,
        city=args.city,
        state=args.state,
        keep=args.keep,
        cache_dir=args.cache_dir,
        tsbpe_file=args.tsbpe_file,
        crawl=not args.no_crawl,
        concurrency=args.concurrency,
        max_pages=args.max_pages,
    )
    write_csv(leads, args.out)
    print(json.dumps(summary, indent=2, default=str))
    print(f"wrote {args.out} ({len(leads)} rows)")
    return 0
