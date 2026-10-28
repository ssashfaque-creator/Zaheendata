from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from zaheendata.http import (
    HostPaused,
    HostUnavailable,
    HttpClient,
    ResponseCache,
    RetryableHTTPError,
    parse_retry_after,
    redact_url,
)
from zaheendata.models import GeoPoint, SourceKind, SourceSearchSpec
from zaheendata.registry import get_registry
from zaheendata.settings import get_settings
from zaheendata.sources import build_sources, describe_sources
from zaheendata.sources.base import SourceContext
from zaheendata.sources.csv_import import CsvImportSource, auto_map, row_to_record
from zaheendata.sources.google_places import SEARCH_URL, GooglePlacesSource, parse_place


def test_blank_api_keys_are_treated_as_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZAHEEN_YELP_API_KEY", "")
    monkeypatch.setenv("ZAHEEN_GOOGLE_MAPS_API_KEY", "   ")
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.secret("yelp_api_key") is None
    assert settings.secret("google_maps_api_key") is None
    described = {s["name"]: s for s in describe_sources(settings)}
    assert described["yelp"]["available"] is False
    assert described["google_places"]["available"] is False
    get_settings.cache_clear()


def test_sources_disabled_without_keys():
    described = {s["name"]: s for s in describe_sources(get_settings())}
    assert described["google_places"]["available"] is False
    assert "ZAHEEN_GOOGLE_MAPS_API_KEY" in described["google_places"]["reason"]
    assert described["google_maps_scraper"]["available"] is False
    assert described["sam_gov"]["available"] is False
    assert "ZAHEEN_SAM_GOV_API_KEY" in described["sam_gov"]["reason"]
    assert described["sba_ppp"]["available"] is False
    assert described["public_datasets"]["available"] is True


def test_build_sources_picks_subsector_cross_checks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZAHEEN_GOOGLE_MAPS_API_KEY", "k")
    monkeypatch.setenv("ZAHEEN_YELP_API_KEY", "k")
    get_settings.cache_clear()
    settings = get_settings()
    ctx = SourceContext(
        http=HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 10)), settings=settings
    )
    names = {s.name for s in build_sources(ctx, get_registry().get("plumbing_hvac"))}
    assert {"google_places", "yelp", "public_datasets"} <= names
    names_gift = {s.name for s in build_sources(ctx, get_registry().get("gift_souvenir_stores"))}
    assert "yelp" not in names_gift and "google_places" in names_gift
    only = {s.name for s in build_sources(ctx, get_registry().get("plumbing_hvac"), only=["yelp"])}
    assert only == {"yelp"}


def test_parse_place():
    place = {
        "id": "ChIJabc",
        "displayName": {"text": "Austin Plumbing"},
        "formattedAddress": "1 Main St, Austin, TX 78701, USA",
        "addressComponents": [
            {"types": ["street_number"], "shortText": "1"},
            {"types": ["route"], "shortText": "Main St"},
            {"types": ["locality"], "shortText": "Austin"},
            {"types": ["administrative_area_level_1"], "shortText": "TX"},
            {"types": ["postal_code"], "shortText": "78701"},
            {"types": ["country"], "shortText": "US"},
        ],
        "location": {"latitude": 30.27, "longitude": -97.74},
        "nationalPhoneNumber": "(512) 555-0100",
        "websiteUri": "https://austinplumbing.com",
        "rating": 4.6,
        "userRatingCount": 123,
        "businessStatus": "OPERATIONAL",
        "primaryType": "plumber",
        "types": ["plumber", "point_of_interest", "establishment"],
        "googleMapsUri": "https://maps.google.com/?cid=1",
    }
    rec = parse_place(place)
    assert rec.source_id == "ChIJabc" and rec.name == "Austin Plumbing"
    assert (
        rec.address.line1 == "1 Main St" and rec.address.region == "TX" and rec.address.postal_code == "78701"
    )
    assert rec.location == GeoPoint(lat=30.27, lng=-97.74)
    assert rec.categories == ["plumber"] and rec.review_count == 123


@respx.mock
async def test_google_places_pagination_and_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("ZAHEEN_GOOGLE_MAPS_API_KEY", "k")
    get_settings.cache_clear()
    settings = get_settings()
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        body = json.loads(request.content)
        assert request.headers["X-Goog-Api-Key"] == "k"
        assert "places.userRatingCount" in request.headers["X-Goog-FieldMask"]
        if body.get("pageToken") == "p2":
            return httpx.Response(200, json={"places": [{"id": "b", "displayName": {"text": "B"}}]})
        return httpx.Response(
            200, json={"places": [{"id": "a", "displayName": {"text": "A"}}], "nextPageToken": "p2"}
        )

    respx.post(SEARCH_URL).mock(side_effect=responder)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 100))
    src = GooglePlacesSource(SourceContext(http=http, settings=settings))
    spec = SourceSearchSpec(
        subsector_key="plumbing_hvac",
        naics="238220",
        queries=["plumber"],
        center=GeoPoint(lat=30.27, lng=-97.74),
        radius_km=20,
        location_text="Austin, TX",
    )
    got = [r async for r in src.search(spec)]
    assert [r.source_id for r in got] == ["a", "b"]
    assert calls["n"] == 2
    # second identical search is served from the on-disk cache: no new HTTP calls, cost unchanged
    got2 = [r async for r in src.search(spec)]
    assert len(got2) == 2 and calls["n"] == 2
    assert http.ledger.calls["google_places.text_search"] == 2
    assert http.ledger.cached["google_places.text_search"] == 2
    assert http.ledger.estimated_usd() == pytest.approx(0.064)
    await http.aclose()


def test_redact_url_strips_api_keys():
    raw = "https://api.sam.gov/entity-information/v3/entities?api_key=secret123&naicsCode=238220"
    assert "secret123" not in redact_url(raw)
    assert "api_key=REDACTED" in redact_url(raw)
    assert "naicsCode=238220" in redact_url(raw)


def test_parse_retry_after():
    assert parse_retry_after(None) is None
    assert parse_retry_after("0") == 0.0
    assert parse_retry_after("30") == 30.0
    assert parse_retry_after("nope") is None


@respx.mock
async def test_http_client_retries_on_429(tmp_path: Path):
    settings = get_settings()
    attempts = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json={"ok": True})

    respx.get("https://api.example.com/x").mock(side_effect=responder)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 100))
    assert await http.get_json("https://api.example.com/x") == {"ok": True}
    assert attempts["n"] == 3
    await http.aclose()


@respx.mock
async def test_http_pauses_host_after_repeated_429(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZAHEEN_HTTP_MAX_RETRIES", "2")
    get_settings.cache_clear()
    settings = get_settings()
    respx.get("https://places.googleapis.com/v1/places:searchText").mock(
        return_value=httpx.Response(429, headers={"retry-after": "0"})
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 100))
    try:
        with pytest.raises(RetryableHTTPError):
            await http.get("https://places.googleapis.com/v1/places:searchText")
        with pytest.raises(RetryableHTTPError):
            await http.get("https://places.googleapis.com/v1/places:searchText")
        with pytest.raises(HostPaused):
            await http.get("https://places.googleapis.com/v1/places:searchText")
        assert http.is_paused("places.googleapis.com")
    finally:
        await http.aclose()
        get_settings.cache_clear()


@respx.mock
async def test_connect_timeout_does_not_pause_host(tmp_path: Path):
    settings = get_settings()
    respx.get("https://html.duckduckgo.com/html/").mock(side_effect=httpx.ConnectTimeout("timed out"))
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 100))
    try:
        with pytest.raises(httpx.ConnectTimeout):
            await http.get("https://html.duckduckgo.com/html/", retries=1, timeout_seconds=1)
        assert not http.is_paused("html.duckduckgo.com")
        with pytest.raises(HostUnavailable):
            await http.get("https://html.duckduckgo.com/html/", retries=1, timeout_seconds=1)
        assert not http.is_paused("html.duckduckgo.com")
        assert http.host_available("html.duckduckgo.com") is False
    finally:
        await http.aclose()


def test_csv_auto_mapping_registry_extract_style():
    headers = [
        "Company Name",
        "Address",
        "City",
        "State",
        "ZIP Code",
        "Phone Number",
        "Web Address",
        "Primary NAICS Code",
        "Location Employees",
        "Executive Name",
        "Executive Title",
        "Email Address",
        "Franchise/Specialty",
    ]
    mapping = auto_map(headers)
    assert mapping["name"] == "Company Name" and mapping["naics"] == "Primary NAICS Code"
    assert mapping["employee_count"] == "Location Employees" and mapping["owner_name"] == "Executive Name"
    row = dict(
        zip(
            headers,
            [
                "Acme Plumbing LLC",
                "1 Main St",
                "Austin",
                "TX",
                "78701",
                "5125550100",
                "acme.com",
                "238220",
                "12",
                "Jane Doe",
                "Owner",
                "jane@acme.com",
                "N",
            ],
            strict=True,
        )
    )
    rec = row_to_record(row, mapping, "import:sos_extract", None, SourceKind.IMPORT)
    assert rec.name == "Acme Plumbing LLC" and rec.employee_count == 12 and rec.naics == "238220"
    assert rec.owner_name == "Jane Doe" and rec.owner_email == "jane@acme.com"
    assert rec.attributes["franchise_flag"] == "N"


async def test_csv_import_source(tmp_path: Path):
    csv_path = tmp_path / "chamber_members.csv"
    csv_path.write_text(
        "Company Name,Website,Phone,City,State,Employees\nFoo Pest,foo.com,512-555-0001,Austin,TX,9\n,,,,,\n"
    )
    settings = get_settings()
    ctx = SourceContext(
        http=HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 10)), settings=settings
    )
    src = CsvImportSource(ctx, csv_path, source_label="import:chamber_members")
    spec = SourceSearchSpec(subsector_key="pest_control", naics="561710", queries=[])
    recs = [r async for r in src.search(spec)]
    assert len(recs) == 1 and recs[0].naics == "561710" and recs[0].employee_count == 9
    await ctx.http.aclose()
