from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from zaheendata.checkpoint import atomic_write_json, json_records_ready
from zaheendata.sources import tdlr


@dataclass(frozen=True)
class LicenseFeed:
    key: str
    states: tuple[str, ...]
    subsectors: tuple[str, ...]
    kind: str
    url: str
    notes: str


# Bulk files first. Add a state here when a daily/weekly CSV exists.
FEEDS: tuple[LicenseFeed, ...] = (
    LicenseFeed(
        key="tx_tdlr_ac",
        states=("TX",),
        subsectors=("plumbing_hvac",),
        kind="soda",
        url="https://data.texas.gov/resource/7358-krk7.json",
        notes="TDLR A/C Contractor. Owner names are people; phones/addresses are blank.",
    ),
    LicenseFeed(
        key="tx_tdlr_electrical",
        states=("TX",),
        subsectors=("electrical",),
        kind="soda",
        url="https://data.texas.gov/resource/7358-krk7.json",
        notes="TDLR Electrical Contractor. Business address and phone are usually populated.",
    ),
    LicenseFeed(
        key="tx_tdlr_salon",
        states=("TX",),
        subsectors=("beauty_salons", "nail_salons"),
        kind="soda",
        url="https://data.texas.gov/resource/7358-krk7.json",
        notes="TDLR cosmetology establishments.",
    ),
    LicenseFeed(
        key="tx_tsbpe_rmp",
        states=("TX",),
        subsectors=("plumbing_hvac",),
        kind="file",
        url="https://tsbpe.texas.gov/free-licensee-list/",
        notes="Download the Responsible Master Plumber CSV and pass --tsbpe-file.",
    ),
    LicenseFeed(
        key="ca_cslb",
        states=("CA",),
        subsectors=("plumbing_hvac", "electrical", "roofing", "remodelers", "painting"),
        kind="manual",
        url="https://www.cslb.ca.gov/",
        notes="California Contractors State License Board publishes licensee extracts.",
    ),
    LicenseFeed(
        key="fl_dbpr",
        states=("FL",),
        subsectors=("plumbing_hvac", "electrical", "beauty_salons"),
        kind="manual",
        url="https://www.myfloridalicense.com/",
        notes="Florida DBPR license download.",
    ),
    LicenseFeed(
        key="ny_dos",
        states=("NY",),
        subsectors=("beauty_salons", "nail_salons"),
        kind="manual",
        url="https://data.ny.gov/",
        notes="NY appearance-enhancement business licenses on data.ny.gov.",
    ),
)


def download_soda_feed(
    feed: LicenseFeed,
    out: Path,
    county: str | None = None,
    *,
    refetch: bool = False,
    max_rows: int = 150_000,
) -> Path:
    if not refetch and json_records_ready(out):
        return out
    mapping = {
        "tx_tdlr_ac": "tdlr_ac_contractor",
        "tx_tdlr_electrical": "tdlr_electrical_contractor",
        "tx_tdlr_salon": "tdlr_cosmetology_establishment",
    }
    filt = tdlr.LICENSE_FILTERS[mapping[feed.key]]
    rows = tdlr.fetch_licenses(license_filter=filt, county=county, max_rows=max_rows)
    records = list(tdlr.iter_match_records(rows, feed.key))
    out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out, records)
    return out


def list_feeds(state: str | None = None, subsector: str | None = None) -> list[LicenseFeed]:
    feeds = list(FEEDS)
    if state:
        feeds = [feed for feed in feeds if state.upper() in feed.states]
    if subsector:
        feeds = [feed for feed in feeds if subsector in feed.subsectors]
    return feeds


def soda_source_keys(state: str | None = None, subsector: str | None = None) -> set[str]:
    return {feed.key for feed in list_feeds(state=state, subsector=subsector) if feed.kind == "soda"}
