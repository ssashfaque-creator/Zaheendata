from zaheendata.cli import build_parser


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
