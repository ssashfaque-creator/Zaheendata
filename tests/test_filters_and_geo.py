from __future__ import annotations

import pytest

from zaheendata.filters import FranchiseDetector, PoorFitFilter
from zaheendata.geo import (
    AreaMatcher,
    GeoPlanner,
    SearchCell,
    grid_cells,
    haversine_km,
    known_place,
    normalize_state,
    parse_location_text,
)
from zaheendata.models import Address, Business, BusinessStatus, GeographySpec, GeoPoint, WebsiteFeatures


def biz(name: str, **kw) -> Business:
    slug = "".join(ch for ch in name.lower() if ch.isalnum())
    defaults = dict(
        website=f"https://{slug}.com",
        domain=f"{slug}.com",
        review_count=100,
        address=Address(city="Austin", region="TX", postal_code="78701"),
        location=GeoPoint(lat=30.27, lng=-97.74),
        business_status="OPERATIONAL",
    )
    defaults.update(kw)
    return Business(name=name, **defaults)


def austin_area() -> AreaMatcher:
    spec = GeographySpec(locations=["Austin, TX"], radius_km=25)
    return AreaMatcher(spec, [SearchCell(GeoPoint(lat=30.2672, lng=-97.7431), 25, "Austin, TX", "TX")])


def test_franchise_brand_and_domain_detection():
    det = FranchiseDetector()
    assert det.reason(biz("Roto-Rooter Plumbing & Water Cleanup")) is not None
    assert det.reason(biz("Great Clips")) is not None
    assert det.reason(biz("Rooter Pros of Austin")) is None  # no false positive on substring
    assert det.reason(biz("Austin Plumbing", domain="austin.mrrooter.com")) is not None


def test_franchise_structural_signals():
    det = FranchiseDetector()
    many = [
        biz(f"Shine Pool Service #{i}", domain="shinepools.com", phone_e164=f"+1512555{i:04d}")
        for i in range(5)
    ]
    det.fit(many)
    assert "domain shared" in (det.reason(many[0]) or "")
    solo = biz("Solo Pools", domain="solopools.com")
    det.fit([solo])
    assert det.reason(solo) is None
    web = biz("Franchise Co", website_features=WebsiteFeatures(crawled=True, franchise_language=True))
    assert det.reason(web) == "website franchise language"
    local = biz(
        "Local Co",
        website_features=WebsiteFeatures(crawled=True, franchise_language=True, locally_owned_language=True),
    )
    assert det.reason(local) is None


def test_poor_fit_filter_reasons():
    f = PoorFitFilter(austin_area())
    items = [
        biz("Good Plumber"),
        biz("No Site", website=None, domain=None),
        biz("Tiny", review_count=4),
        biz(
            "Dallas HVAC",
            location=GeoPoint(lat=32.78, lng=-96.8),
            address=Address(city="Dallas", region="TX"),
        ),
        biz("Closed", business_status="CLOSED_PERMANENTLY"),
        biz("Orkin Pest Control"),
    ]
    reasons = f.apply(items)
    assert items[0].status == BusinessStatus.CANDIDATE
    assert items[1].rejection_reason == "no website"
    assert items[2].rejection_reason.startswith("too few reviews")
    assert items[3].rejection_reason.startswith("outside target area")
    assert items[4].rejection_reason.startswith("closed")
    assert items[5].rejection_reason.startswith("franchise/chain")
    assert reasons == {
        "no website": 1,
        "too few reviews": 1,
        "outside target area": 1,
        "closed": 1,
        "franchise/chain": 1,
    }


def test_employee_range_filter_opt_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZAHEEN_ENFORCE_EMPLOYEE_RANGE", "true")
    from zaheendata.settings import get_settings

    get_settings.cache_clear()
    f = PoorFitFilter(None, get_settings())
    assert f.reject_reason(biz("Big", employee_count=120)).startswith("employee count")
    assert f.reject_reason(biz("Right size", employee_count=12)) is None
    assert f.reject_reason(biz("Unknown size")) is None


def test_geo_helpers():
    assert (
        normalize_state("Texas") == "TX"
        and normalize_state("tx") == "TX"
        and normalize_state("Nowhere") is None
    )
    assert parse_location_text("Austin, TX") == ("Austin", "TX")
    assert parse_location_text("Austin Texas") == ("Austin", "TX")
    austin = known_place("Austin, TX")
    assert austin is not None and abs(austin.lat - 30.2672) < 0.001
    assert (
        round(haversine_km(GeoPoint(lat=30.2672, lng=-97.7431), GeoPoint(lat=32.7767, lng=-96.797)), 0) == 293
    )
    cells = grid_cells(30.0, -98.0, 31.0, -97.0, 40.0, "x", "TX")
    assert 4 <= len(cells) <= 12
    assert all(c.radius_km > 20 for c in cells)


async def test_geo_planner_scopes():
    planner = GeoPlanner(geocoder=None)
    state_cells = await planner.plan(GeographySpec(state="RI", grid_cell_km=40))
    assert 1 <= len(state_cells) <= 12 and all(c.region == "RI" for c in state_cells)
    national = await planner.plan(GeographySpec(grid_cell_km=200))
    assert len({c.region for c in national}) == 51
    with pytest.raises(ValueError):
        await planner.plan(GeographySpec(state="ZZ"))


def test_area_matcher_state_scope():
    spec = GeographySpec(state="TX")
    matcher = AreaMatcher(spec, [SearchCell(GeoPoint(lat=31, lng=-99), 30, "Texas", "TX")])
    assert matcher.inside(None, "TX") == (True, None)
    assert matcher.inside(None, "OK")[0] is False
    assert matcher.inside(None, None) == (True, None)
