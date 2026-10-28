from __future__ import annotations

import json
from pathlib import Path

from zaheendata.models import Address, RawRecord, SourceKind
from zaheendata.normalize import (
    extract_domain,
    is_social_or_directory_url,
    normalize_name,
    normalize_phone,
    normalize_record,
    normalize_url,
)
from zaheendata.resolve import cluster_records, resolve


def test_phone_normalisation():
    assert normalize_phone("(512) 555-0101") == "+15125550101"
    assert normalize_phone("512.555.0101") == "+15125550101"
    assert normalize_phone("+1 512-555-0101") == "+15125550101"
    assert normalize_phone("call us") is None
    assert normalize_phone(None) is None


def test_url_and_domain():
    assert normalize_url("hillcountryplumbingair.com") == "http://hillcountryplumbingair.com/"
    assert extract_domain("https://www.Example.com/path?x=1") == "example.com"
    assert extract_domain("not a url") is None
    assert is_social_or_directory_url("https://www.facebook.com/somebiz")
    assert not is_social_or_directory_url("https://somebiz.com")


def test_name_normalisation():
    assert normalize_name("Hill Country Plumbing & Air, LLC") == "hill country plumbing air"
    assert normalize_name("HILL COUNTRY PLUMBING AND AIR") == "hill country plumbing air"
    assert normalize_name("Jose's Plumbing Co.") == "joses plumbing"


def test_normalize_record_moves_social_profile_out_of_website():
    rec = RawRecord(
        source="x",
        source_kind=SourceKind.MAPS,
        name=" Foo  Bar ",
        website="https://instagram.com/foobar",
        phone="512-555-0100",
        address=Address(formatted="1 Main St, Austin, TX 78701"),
    )
    out = normalize_record(rec)
    assert out.name == "Foo Bar"
    assert out.website is None
    assert out.attributes["social_profile_url"].startswith("https://instagram.com")
    assert out.phone == "+15125550100"
    assert out.address and out.address.region == "TX" and out.address.postal_code == "78701"


def _fixture_records(path: Path) -> list[RawRecord]:
    data = json.loads(path.read_text())
    out = []
    for item in data:
        if item.get("subsector_key") != "plumbing_hvac":
            continue
        item = {k: v for k, v in item.items() if k != "subsector_key"}
        for r in item.get("reviews", []):
            r.pop("days_ago", None)
        out.append(RawRecord(**item))
    return out


def test_resolution_merges_same_business_across_sources(fixtures_dir: Path):
    records = _fixture_records(fixtures_dir / "records.json")
    clusters = cluster_records([normalize_record(r) for r in records])
    sizes = sorted(len(c) for c in clusters)
    # Hill Country appears 3x (google, apollo, licence); Lone Star 2x (google, osm)
    assert sizes[-2:] == [2, 3]

    businesses = resolve(records, {"google_places": 80, "apollo": 70})
    hill = next(b for b in businesses if "hill country" in b.normalized_name)
    assert hill.name == "Hill Country Plumbing & Air"  # consumer-facing maps name wins
    assert hill.phone_e164 == "+15125550101"
    assert hill.domain == "hillcountryplumbingair.com"
    assert hill.employee_count == 14  # vendor field
    assert hill.review_count == 212  # maps field
    assert hill.license_number == "TACLA0001"
    assert hill.owner.name == "Marcus Reyes"
    assert set(hill.sources) == {"google_places", "apollo", "license:tx_tdlr_licenses"}
    assert hill.google_place_id == "ChIJ_hill_country"


def test_resolution_does_not_merge_distinct_businesses():
    a = RawRecord(
        source="google_places",
        source_kind=SourceKind.MAPS,
        name="ABC Plumbing",
        phone="512-555-0001",
        address=Address(postal_code="78701"),
    )
    b = RawRecord(
        source="google_places",
        source_kind=SourceKind.MAPS,
        name="XYZ Electric",
        phone="512-555-0002",
        address=Address(postal_code="78701"),
    )
    assert len(resolve([a, b])) == 2


def test_shared_domain_with_different_names_stays_separate():
    a = RawRecord(
        source="g",
        source_kind=SourceKind.MAPS,
        name="Mr. Rooter of Austin",
        website="https://mrrooter.com/austin",
    )
    b = RawRecord(
        source="g",
        source_kind=SourceKind.MAPS,
        name="Mr. Rooter of Dallas",
        website="https://mrrooter.com/dallas",
    )
    c = RawRecord(
        source="g",
        source_kind=SourceKind.MAPS,
        name="Mister Rooter Austin",
        website="https://mrrooter.com/austin",
    )
    merged = resolve([a, b, c])
    # a and c are near-identical names on one domain -> merged; b remains separate
    assert len(merged) == 2
