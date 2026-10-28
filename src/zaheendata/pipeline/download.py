from __future__ import annotations

from pathlib import Path

from zaheendata.geo import parquet_stem, state_from_geo
from zaheendata.sources.licenses_catalog import download_soda_feed, list_feeds
from zaheendata.sources.overture import download_places
from zaheendata.sources.ppp import download_ppp
from zaheendata.subsectors import get_subsector


def download_overture(
    *,
    geo: str,
    subsector_key: str | None,
    raw_dir: Path = Path("data/raw"),
    limit: int | None = None,
    out: Path | None = None,
) -> Path:
    dest = out or raw_dir / f"{parquet_stem(geo, subsector_key)}.parquet"
    return download_places(geo=geo, subsector_key=subsector_key, out=dest, limit=limit)


def download_licenses_for(
    *,
    geo: str,
    subsector_key: str,
    raw_dir: Path = Path("data/raw"),
    county: str | None = None,
) -> list[Path]:
    state = state_from_geo(geo)
    written: list[Path] = []
    for feed in list_feeds(state=state, subsector=subsector_key):
        if feed.kind != "soda":
            continue
        dest = raw_dir / f"{feed.key}.json"
        download_soda_feed(feed, dest, county=county)
        written.append(dest)
    return written


def download_ppp_for(
    *,
    subsector_key: str,
    geo: str,
    raw_dir: Path = Path("data/raw"),
    plus_only: bool = True,
) -> Path:
    spec = get_subsector(subsector_key)
    dest = raw_dir / f"ppp_{spec.naics}_{state_from_geo(geo) or 'US'}.parquet"
    return download_ppp(out=dest, naics=spec.naics, state=state_from_geo(geo), plus_only=plus_only)
