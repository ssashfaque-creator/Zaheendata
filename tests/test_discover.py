"""Free website / address discovery (DuckDuckGo + Nominatim) and harvest from crawled pages."""

from __future__ import annotations

from pathlib import Path

import httpx
import respx

from zaheendata.enrich.discover import (
    DirectoryDiscoverer,
    completeness,
    distinctive_tokens,
    domain_stems,
    harvest_from_website,
    page_matches_business,
    parse_ddg_html,
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


def test_domain_stems_skip_generic_only_names():
    assert "airco" in domain_stems("Airco Mechanical")
    assert distinctive_tokens("Cooling Solutions LLC") == []
    assert page_matches_business("Welcome to Airco Mechanical in Austin", "Airco Mechanical", "Austin")
    assert not page_matches_business("Buy this domain at GoDaddy", "Airco Mechanical", "Austin")
    assert not page_matches_business("A generic cooling company", "Cooling Solutions LLC", "Austin")


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


@respx.mock
async def test_discoverer_uses_nominatim_then_duckduckgo(tmp_path: Path):
    settings = get_settings()
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
    assert b.address and b.address.line1 == "1200 S Lamar Blvd"
    assert b.location and abs(b.location.lat - 30.25) < 0.01


@respx.mock
async def test_discoverer_falls_back_to_duckduckgo(tmp_path: Path):
    settings = get_settings()
    respx.get(url__regex=r"https://nominatim\.openstreetmap\.org/search.*").mock(
        return_value=httpx.Response(200, json=[])
    )
    def ddg(request: httpx.Request) -> httpx.Response:
        assert "Mozilla" in request.headers.get("user-agent", "")
        return httpx.Response(200, text=DDG_HTML)

    respx.get(url__regex=r"https://html\.duckduckgo\.com/html/.*").mock(side_effect=ddg)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "c.sqlite", 60))
    disc = DirectoryDiscoverer(http, settings)
    disc.fallback_where = "Austin, TX"
    b = Business(name="1ST CALL PLUMBING HEATING & AIR LTD")
    await disc.fill(b)
    await http.aclose()
    assert b.website and "1stcallplumbing.com" in b.website
    assert b.attributes.get("website_source") == "duckduckgo"
