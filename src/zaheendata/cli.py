from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from zaheendata.pipeline.diagnose import diagnose_csv, render_report
from zaheendata.pipeline.export import write_csv
from zaheendata.pipeline.run import run_pipeline
from zaheendata.subsectors import SUBSECTORS


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zaheendata",
        description="Build a qualified local-service lead list from public sources.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Discover, filter, crawl, score, export.")
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


def main(argv: list[str] | None = None) -> int:
    _load_dotenv(Path(".env"))
    args = build_parser().parse_args(argv)
    if args.command == "subsectors":
        for key, spec in SUBSECTORS.items():
            area = "state/national" if spec.wide_area else "metro"
            print(f"{key:20} NAICS {spec.naics}  {spec.name}  [{area}]")
            print(f"{'':20} license: {spec.license_source}")
            print(f"{'':20} signal:  {spec.fit_signal}")
        return 0
    if args.command == "diagnose":
        print(render_report(diagnose_csv(args.csv_path)))
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
