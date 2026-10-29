from pathlib import Path

from zaheendata.cli import apply_harvest_layout, build_parser


def test_parser_exposes_download_first_commands() -> None:
    parser = build_parser()
    args = parser.parse_args(["build", "--subsector", "plumbing_hvac", "--geo", "US", "--crawl", "none"])
    assert args.command == "build"
    assert args.geo == "US"
    assert args.crawl == "none"

    down = parser.parse_args(["download", "places", "--geo", "TX", "--subsector", "electrical"])
    assert down.source == "places"
    assert down.subsector == "electrical"

    stats = parser.parse_args(["stats"])
    assert stats.command == "stats"
    doctor = parser.parse_args(["doctor"])
    assert doctor.command == "doctor"

    harvest = parser.parse_args(["harvest", "--geo", "TX", "--fresh", "--crawl", "none"])
    assert harvest.command == "harvest"
    assert harvest.geo == "TX"
    assert harvest.fresh is True
    assert harvest.crawl == "none"
    pool = parser.parse_args(["harvest", "--geo", "TX", "--workers", "4", "--concurrency", "8"])
    assert pool.workers == 4
    assert pool.concurrency == 8

    reset = parser.parse_args(["reset"])
    assert reset.command == "reset"

    georgia = parser.parse_args(["harvest", "--geo", "GA", "--workers", "4", "--concurrency", "8"])
    apply_harvest_layout(georgia)
    assert georgia.geo == "GA"
    assert georgia.warehouse == Path("data/GA/warehouse.duckdb")
    assert georgia.raw_dir == Path("data/GA/raw")
    assert georgia.out_dir == Path("data/GA/exports")
    assert georgia.state == Path("data/GA/harvest_state.json")
    assert georgia.log == Path("data/GA/harvest.log")

    worker = parser.parse_args(
        [
            "harvest",
            "--geo",
            "GA",
            "--warehouse",
            "data/GA/workers/w1.duckdb",
            "--raw-dir",
            "data/GA/raw",
            "--out-dir",
            "data/GA/exports",
            "--skip-setup",
            "--state",
            "data/GA/workers/w1_state.json",
            "--log",
            "data/GA/workers/w1.log",
        ]
    )
    apply_harvest_layout(worker)
    assert worker.warehouse == Path("data/GA/workers/w1.duckdb")
    assert worker.state == Path("data/GA/workers/w1_state.json")

    stats = parser.parse_args(["stats", "--geo", "GA"])
    assert stats.geo == "GA"
