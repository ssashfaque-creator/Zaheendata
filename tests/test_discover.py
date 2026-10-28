"""Free website / address discovery (DuckDuckGo + Nominatim) and harvest from crawled pages."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from zaheendata.enrich.discover import (
    DirectoryDiscoverer,
    census_point,
    completeness,
    harvest_from_website,
    parse_bing_html,
    parse_census_batch,
    parse_ddg_api,
    parse_ddg_html,
    photon_pick,
    pick_website,
)
from zaheendata.http import HttpClient, ResponseCache
from zaheendata.models import Address, Business, OwnerContact, WebsiteFeatures
from zaheendata.settings import get_settings

DDG_HTML = (
    "<html><body>"
    '<a class="result__a" href="https://duckduckgo.com/l/?uddg='
    'https%3A%2F%2Fwww.1stcallplumbing.com%2F">1st Call</a>'
    '<a class="result__a" href="https://www.yelp.com/biz/1st-call-plumbing">Yelp</a>'
    '<a class="result__a" href="https://www.facebook.com/1stcall">Facebook</a>'
    "</body></html>"
)


def test_parse_census_batch_maps_id_to_point():
    csv_body = (
        '"abc","1200 S Lamar Blvd, Austin, TX, 78704","Match","Exact",'
        '"1200 S LAMAR BLVD, AUSTIN, TX, 78704","-97.75,30.25","1","L"\n'
        '"nope","x","No_Match","",,,,\n'
    )
    points = parse_census_batch(csv_body)
    assert "abc" in points and abs(points["abc"].lat - 30.25) < 0.001
    assert "nope" not in points


def test_census_point_reads_coordinates():
    point = census_point({"result": {"addressMatches": [{"coordinates": {"x": -97.75, "y": 30.25}}]}})
    assert point is not None and abs(point.lat - 30.25) < 0.001 and abs(point.lng + 97.75) < 0.001
    assert census_point({"result": {"addressMatches": []}}) is None


def test_photon_pick_requires_name_and_city_match():
    airco = {
        "properties": {"name": "Airco Mechanical", "city": "Austin", "state": "Texas"},
        "geometry": {"coordinates": [-97.7, 30.3]},
    }
    other_city = {
        "properties": {"name": "Airco Mechanical", "city": "Dallas", "state": "Texas"},
        "geometry": {"coordinates": [-96.8, 32.8]},
    }
    airport = {
        "properties": {"name": "Austin-Bergstrom International Airport", "city": "Austin", "state": "Texas"},
        "geometry": {"coordinates": [-97.6, 30.2]},
    }
    assert photon_pick("Airco Mechanical", "Austin", "TX", [airport, airco])["properties"]["city"] == "Austin"
    assert photon_pick("Airco Mechanical", "Austin", "TX", [other_city]) is None
    assert photon_pick("Airco Mechanical", "Austin", "TX", [airport]) is None


def test_parse_ddg_api_and_bing_html():
    api_urls = parse_ddg_api(
        {
            "AbstractURL": "https://www.1stcallplumbing.com/",
            "RelatedTopics": [{"FirstURL": "https://www.yelp.com/biz/x"}],
        }
    )
    assert any("1stcallplumbing.com" in u for u in api_urls)
    bing = parse_bing_html(
        '<ol id="b_results"><li class="b_algo"><h2>'
        '<a href="https://www.1stcallplumbing.com/">1st Call</a></h2></li></ol>'
    )
    assert any("1stcallplumbing.com" in u for u in bing)


def test_parse_ddg_and_pick_website():
    urls = parse_ddg_html(DDG_HTML)
    assert any("1stcallplumbing.com" in u for u in urls)
    chosen = pick_website("1ST CALL PLUMBING HEATING & AIR LTD", urls)
    assert chosen and "1stcallplumbing.com" in chosen
    assert pick_website("1ST CALL PLUMBING", ["https://www.yelp.com/biz/x"]) is None


def test_harvest_from_website_fills_gaps():
    b = Business(
        name="Acme HVAC",
        address=Address(city="Austin", region="TX"),
        owner=OwnerContact(),
        website_features=WebsiteFeatures(
            crawled=True,
            phones=["5125550199"],
            emails=["bob@acmehvac.com"],
            owner_name_candidates=["Bob Smith"],
            street_address="100 Main St",
            address_postal="78701",
        ),
    )
    harvest_from_website(b)
    assert b.phone_e164 == "+15125550199"
    assert b.owner.name == "Bob Smith" and b.owner.email == "bob@acmehvac.com"
    assert b.address and b.address.line1 == "100 Main St" and b.address.postal_code == "78701"


def test_completeness_ranks_crawled_sites_first():
    empty = Business(name="A")
    rich = Business(
        name="B",
        website="https://b.com",
        phone_e164="+15125550100",
        address=Address(line1="1 Main", city="Austin"),
        website_features=WebsiteFeatures(crawled=True),
    )
    assert completeness(rich) > completeness(empty)


def _empty_photon() -> httpx.Response:
    return httpx.Response(200, json={"features": []})


def _empty_ddg(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html></html>")


@respx.mock
async def test_discoverer_uses_photon_without_nominatim(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://photon\.komoot\.io/api/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "properties": {
                            "name": "Hill Country Plumbing & Air",
                            "city": "Austin",
                            "state": "Texas",
                            "housenumber": "1200",
                            "street": "S Lamar Blvd",
                            "postcode": "78704",
                            "osm_type": "N",
                            "osm_id": 42,
                        },
                        "geometry": {"coordinates": [-97.75, 30.25]},
                    }
                ]
            },
        )
    )
    respx.get(url__regex=r"https://geocoding\.geo\.census\.gov/.*").mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": []}})
    )
    respx.get(url__regex=r"https://(html|lite)\.duckduckgo\.com/.*").mock(side_effect=_empty_ddg)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(
        name="Hill Country Plumbing & Air",
        address=Address(city="Austin", region="TX"),
    )
    found = await disc.fill(b)
    await http.aclose()
    assert found.get("poi") == "photon"
    assert b.attributes.get("osm_id") == 42
    assert b.address and b.address.line1 == "1200 S Lamar Blvd"
    assert b.location and abs(b.location.lat - 30.25) < 0.01
    assert not any("nominatim" in str(call.request.url) for call in respx.calls)


@respx.mock
async def test_discoverer_nominatim_is_last_resort(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://photon\.komoot\.io/api/.*").mock(return_value=_empty_photon())
    respx.get(url__regex=r"https://geocoding\.geo\.census\.gov/.*").mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": []}})
    )
    respx.get(url__regex=r"https://(html|lite)\.duckduckgo\.com/.*").mock(side_effect=_empty_ddg)
    respx.get(url__regex=r"https://nominatim\.openstreetmap\.org/search.*").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "display_name": "Hill Country Plumbing, Austin, Texas",
                    "lat": "30.25",
                    "lon": "-97.75",
                    "address": {
                        "house_number": "1200",
                        "road": "S Lamar Blvd",
                        "city": "Austin",
                        "ISO3166-2-lvl4": "US-TX",
                        "postcode": "78704",
                    },
                    "extratags": {
                        "website": "https://www.hillcountryplumbingair.com",
                        "phone": "512-555-0101",
                    },
                }
            ],
        )
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(
        name="Hill Country Plumbing & Air",
        address=Address(line1="1200 S Lamar Blvd", city="Austin", region="TX"),
    )
    found = await disc.fill(b)
    await http.aclose()
    assert found.get("geo") == "nominatim"
    assert b.website and "hillcountryplumbingair.com" in b.website
    assert b.phone_e164 == "+15125550101"
    assert b.location and abs(b.location.lat - 30.25) < 0.01


@respx.mock
async def test_discoverer_falls_back_to_duckduckgo(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://photon\.komoot\.io/api/.*").mock(return_value=_empty_photon())
    respx.get(url__regex=r"https://nominatim\.openstreetmap\.org/search.*").mock(
        return_value=httpx.Response(200, json=[])
    )

    def ddg(request: httpx.Request) -> httpx.Response:
        assert "Mozilla" in request.headers.get("user-agent", "")
        return httpx.Response(200, text=DDG_HTML)

    respx.get(url__regex=r"https://(html|lite)\.duckduckgo\.com/.*").mock(side_effect=ddg)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(name="1ST CALL PLUMBING HEATING & AIR LTD")
    await disc.fill(b)
    await http.aclose()
    assert b.website and "1stcallplumbing.com" in b.website
    assert b.attributes.get("website_source") == "duckduckgo"


@respx.mock
async def test_geocode_batch_parses_census_csv(tmp_path: Path):
    settings = get_settings()
    b = Business(
        name="Airco",
        address=Address(line1="100 Main St", city="Austin", region="TX", postal_code="78701"),
    )

    def batch(request: httpx.Request) -> httpx.Response:
        body = (
            f'"{b.id}","100 Main St, Austin, TX, 78701","Match","Exact",'
            f'"100 MAIN ST, AUSTIN, TX, 78701","-97.74,30.27","1","L"\n'
        )
        return httpx.Response(200, text=body)

    respx.post(url__regex=r"https://geocoding\.geo\.census\.gov/geocoder/locations/addressbatch.*").mock(
        side_effect=batch
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    n = await disc.geocode_batch([b])
    await http.aclose()
    assert n == 1
    assert b.location and abs(b.location.lat - 30.27) < 0.01
    assert b.attributes.get("geo_source") == "census"


@respx.mock
async def test_apply_osm_contacts_from_overpass(tmp_path: Path):
    settings = get_settings()
    respx.post(url__regex=r"https://overpass.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "elements": [
                    {
                        "type": "node",
                        "id": 42,
                        "tags": {"website": "https://www.airco.com", "phone": "512-555-0199"},
                    }
                ]
            },
        )
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    b = Business(name="Airco Mechanical", attributes={"osm_type": "N", "osm_id": 42})
    n = await disc.apply_osm_contacts([b])
    await http.aclose()
    assert n == 1
    assert b.website and "airco.com" in b.website
    assert b.phone_e164 == "+15125550199"


@respx.mock
async def test_photon_fails_over_to_a_mirror(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://photon\.komoot\.io/api/.*").mock(return_value=httpx.Response(503))
    respx.get(url__regex=r"https://photon\.koalasec\.org/api/.*").mock(
        return_value=httpx.Response(
            200,
            json={
                "features": [
                    {
                        "properties": {
                            "name": "Airco Mechanical",
                            "city": "Austin",
                            "state": "Texas",
                            "osm_type": "N",
                            "osm_id": 7,
                        },
                        "geometry": {"coordinates": [-97.7, 30.3]},
                    }
                ]
            },
        )
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(name="Airco Mechanical", address=Address(city="Austin", region="TX"))
    assert await disc.fill_photon_many([b]) == 1
    await http.aclose()
    assert b.attributes.get("osm_id") == 7
    assert not http.is_paused("photon.komoot.io")


@respx.mock
async def test_website_search_falls_back_to_bing_when_ddg_times_out(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://(html|lite|api)\.duckduckgo\.com/.*").mock(
        side_effect=httpx.ConnectTimeout("timed out")
    )
    respx.get(url__regex=r"https://www\.bing\.com/search.*").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<ol id="b_results"><li class="b_algo"><h2>'
                '<a href="https://www.1stcallplumbing.com/">1st Call</a></h2></li></ol>'
            ),
        )
    )
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(name="1ST CALL PLUMBING HEATING & AIR LTD")
    assert await disc.fill_website(b) is True
    await http.aclose()
    assert b.website and "1stcallplumbing.com" in b.website
    assert not http.is_paused("html.duckduckgo.com")
    assert not http.is_paused("lite.duckduckgo.com")
