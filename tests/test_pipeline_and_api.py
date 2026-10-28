from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from zaheendata import api as api_mod
from zaheendata.export import COLUMNS, LEAD_COLUMNS, flatten, to_csv, to_xlsx
from zaheendata.http import HttpClient, ResponseCache
from zaheendata.models import BusinessStatus, CampaignSpec, CampaignState, GeographySpec
from zaheendata.pipeline import CampaignRunner
from zaheendata.settings import get_settings
from zaheendata.sources.base import SourceContext
from zaheendata.sources.fixture import FixtureSource
from zaheendata.storage import Database
from zaheendata.testing import FixtureTransport


@pytest.fixture
async def env(tmp_path: Path, fixtures_dir: Path):
    settings = get_settings()
    db = Database(settings)
    await db.init()
    http = HttpClient(
        settings, cache=ResponseCache(tmp_path / "cache.sqlite", 60), transport=FixtureTransport(fixtures_dir)
    )
    yield settings, db, http, fixtures_dir
    await http.aclose()
    await db.close()


async def _run(env, subsector: str = "plumbing_hvac", **spec_kw):
    settings, db, http, fixtures_dir = env
    ctx = SourceContext(http=http, settings=settings)
    spec = CampaignSpec(
        name="t",
        subsector_key=subsector,
        geography=GeographySpec(locations=["Austin, TX"]),
        sources=[],
        short_list_target=3,
        **spec_kw,
    )
    campaign = await db.create_campaign(spec)
    events: list[str] = []
    runner = CampaignRunner(
        db,
        http,
        settings,
        extra_sources=[FixtureSource(ctx, fixtures_dir / "records.json")],
        progress=lambda stage, data: events.append(stage),
    )
    result = await runner.run(campaign)
    return db, result, events


def test_resume_cells_do_not_need_nominatim():
    from zaheendata.pipeline import CampaignRunner

    cells = CampaignRunner._resume_cells(GeographySpec(locations=["Austin, TX"], radius_km=25))
    assert len(cells) == 1
    assert cells[0].center.lat == 0 and cells[0].center.lng == 0
    assert cells[0].region == "TX"
    assert cells[0].label == "Austin, TX"


async def test_end_to_end_pipeline(env):
    db, result, events = await _run(env)
    assert result.state == CampaignState.COMPLETED
    assert events[0] == "run.start"
    assert "enrich.start" in events
    assert "enrich.progress" in events
    assert [e for e in events if "." not in e] == [
        "collect",
        "resolve",
        "filter",
        "enrich",
        "score",
        "maps",
        "contacts",
        "finalize",
    ]
    stats = result.stats
    assert stats["raw_records"] == 12 and stats["businesses"] == 9
    assert stats["filter_rejections"] == {
        "franchise/chain": 1,
        "outside target area": 1,
        "closed": 1,
        "too few reviews": 1,
    }
    assert stats["websites_crawled"] == 4
    assert stats["selected"] == 3

    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert [b.name for b in selected] == [
        "Hill Country Plumbing & Air",
        "Lone Star Comfort Systems",
        "Eastside Plumbing Co",
    ]
    top = selected[0]
    assert top.score and top.score.total == 10 and top.score.rank == 1
    assert top.owner.name == "Marcus Reyes" and top.owner.email == "marcus@hillcountryplumbingair.com"
    assert top.website_features.maintenance_plan and top.website_features.emergency_service
    assert {"Square", "Mailchimp"} <= top.tech_stack.names
    assert top.review_insights.recent_reviews == 4 and top.review_insights.pain_mentions >= 1

    scored = await db.list_businesses(result.id, status=BusinessStatus.SCORED)
    assert {b.name for b in scored} == {"Barton Springs Plumbing", "Jose's Plumbing Service"}
    rejected = await db.list_businesses(result.id, status=BusinessStatus.REJECTED)
    assert {b.name: b.rejection_reason.split(":")[0] for b in rejected} == {
        "Roto-Rooter Plumbing & Water Cleanup": "franchise/chain",
        "Capital City Drain Pros": "too few reviews",
        "Big D Heating & Air": "outside target area",
        "Old Town Plumbing": "closed",
    }
    counts = await db.business_counts(result.id)
    assert counts == {"selected": 3, "scored": 2, "rejected": 4}


async def test_pipeline_resume_from_score(env):
    db, result, _ = await _run(env)
    settings, _db, http, _ = env
    # change the shortlist size and resume at the scoring stage without re-collecting
    result.spec.short_list_target = 1
    runner = CampaignRunner(db, http, settings)
    again = await runner.run(result, start_stage="score")
    assert again.state == CampaignState.COMPLETED
    assert again.stats["raw_records"] == 12  # preserved from the first run
    assert (await db.business_counts(result.id))["selected"] == 1


async def test_export_formats(env):
    db, result, _ = await _run(env)
    items = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    await db.add_suppressions("domain", ["lonestarcomfort.com"], "existing customer")
    sup = await db.suppression_sets()
    rows = [flatten(b, sup) for b in items]
    assert rows[0]["rank"] == 1 and rows[0]["owner_email"] == "marcus@hillcountryplumbingair.com"
    assert rows[1]["suppressed"] is True and "domain suppressed" in rows[1]["suppression_reasons"]
    csv_text = to_csv(items, sup)
    assert csv_text.splitlines()[0].split(",") == COLUMNS
    assert COLUMNS[:3] == ["name", "phone", "website"]
    assert LEAD_COLUMNS[0] == "name"
    assert len(csv_text.splitlines()) == 4
    assert to_xlsx(items, sup)[:2] == b"PK"
    assert flatten(items[0])["website_crawled"] is True


async def test_vendor_only_campaign_skips_review_rule(env, tmp_path: Path):
    """With no maps source configured, nothing carries review counts; the <10 reviews rule must
    not wipe out the vendor long list. The run is flagged so the operator knows."""
    from zaheendata.sources.csv_import import CsvImportSource

    settings, db, http, _ = env
    csv_path = tmp_path / "vendor.csv"
    csv_path.write_text(
        "Company Name,City,State,Phone,Website,Primary NAICS Code,Location Employees,Executive Name\n"
        "Hill Country Plumbing & Air,Austin,TX,5125550101,hillcountryplumbingair.com,238220,14,Marcus Reyes\n"
        "Roto-Rooter,Austin,TX,5125550103,rotorooter.com,238220,12,\n"
    )
    ctx = SourceContext(http=http, settings=settings)
    spec = CampaignSpec(
        name="v",
        subsector_key="plumbing_hvac",
        geography=GeographySpec(locations=["Austin, TX"]),
        sources=[],
        contact_enrichment=False,
    )
    campaign = await db.create_campaign(spec)
    runner = CampaignRunner(
        db, http, settings, extra_sources=[CsvImportSource(ctx, csv_path, "import:vendor")]
    )
    result = await runner.run(campaign)
    assert result.stats["review_filter_skipped"] is True
    assert result.stats["filter_rejections"] == {"franchise/chain": 1}
    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert [b.name for b in selected] == ["Hill Country Plumbing & Air"]
    assert selected[0].score.by_name()["busy_and_growing"] == 0  # no review data: no credit
    assert selected[0].website_features.maintenance_plan  # website enrichment still ran


REGISTRY_CSV = (
    "Company Name,Address,City,State,ZIP Code,Primary NAICS Code,Location Employees,Owner\n"
    "HILL COUNTRY PLUMBING AND AIR LLC,1200 S LAMAR BLVD,AUSTIN,TX,78704,238220,14,Marcus Reyes\n"
    "ROTO-ROOTER PLUMBING,900 CONGRESS AVE,AUSTIN,TX,78701,238220,12,\n"
    "LONE STAR MECHANICAL SERVICES LLC,500 CONGRESS AVE,AUSTIN,TX,78701,238220,9,\n"
)


async def test_registry_only_campaign_without_maps_skips_website_rule(env, tmp_path: Path):
    """Government registries carry no websites or reviews. With no Maps key those two rules would
    reject everything, so they are skipped and the run is flagged for the operator."""
    from zaheendata.models import SourceKind
    from zaheendata.sources.csv_import import CsvImportSource

    settings, db, http, _ = env
    csv_path = tmp_path / "registry.csv"
    csv_path.write_text(REGISTRY_CSV)
    ctx = SourceContext(http=http, settings=settings)
    spec = CampaignSpec(
        name="r",
        subsector_key="plumbing_hvac",
        geography=GeographySpec(locations=["Austin, TX"]),
        sources=[],
        contact_enrichment=False,
    )
    campaign = await db.create_campaign(spec)
    source = CsvImportSource(ctx, csv_path, "registry:test", kind=SourceKind.REGISTRY)
    result = await CampaignRunner(db, http, settings, extra_sources=[source]).run(campaign)
    assert result.stats["website_filter_skipped"] is True
    assert result.stats["review_filter_skipped"] is True
    assert result.stats["maps_match_pending"] == 0  # no Places source: nothing to defer to
    assert result.stats["filter_rejections"] == {"franchise/chain": 1}
    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert {b.name for b in selected} == {
        "HILL COUNTRY PLUMBING AND AIR LLC",
        "LONE STAR MECHANICAL SERVICES LLC",
    }


class _PlacesFixtureTransport(FixtureTransport):
    """FixtureTransport plus a Places Text Search that only knows one listing, found by name."""

    def __init__(self, root: Path):
        super().__init__(root)
        self.text_queries: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "places.googleapis.com":
            import json

            body = json.loads(request.content)
            self.text_queries.append(body["textQuery"])
            places = []
            if body["textQuery"].lower().startswith("hill country plumbing"):
                places = [
                    {
                        "id": "ChIJ_hill_country",
                        "displayName": {"text": "Hill Country Plumbing & Air"},
                        "formattedAddress": "1200 S Lamar Blvd, Austin, TX 78704, USA",
                        "addressComponents": [
                            {"types": ["locality"], "shortText": "Austin"},
                            {"types": ["administrative_area_level_1"], "shortText": "TX"},
                            {"types": ["postal_code"], "shortText": "78704"},
                        ],
                        "location": {"latitude": 30.253, "longitude": -97.763},
                        "nationalPhoneNumber": "(512) 555-0101",
                        "websiteUri": "https://www.hillcountryplumbingair.com",
                        "rating": 4.8,
                        "userRatingCount": 212,
                        "businessStatus": "OPERATIONAL",
                        "types": ["plumber"],
                    },
                    {  # a different company that happens to come back for the same query
                        "id": "ChIJ_other",
                        "displayName": {"text": "Hill Country Bakery"},
                        "addressComponents": [{"types": ["administrative_area_level_1"], "shortText": "TX"}],
                        "websiteUri": "https://hcbakery.com",
                        "userRatingCount": 900,
                    },
                ]
            return httpx.Response(200, json={"places": places})
        if request.url.host == "maps.googleapis.com" and request.url.path.endswith("/details/json"):
            return httpx.Response(
                200, json={"result": {"rating": 4.8, "user_ratings_total": 212, "reviews": []}}
            )
        return await super().handle_async_request(request)


async def test_registry_records_are_matched_to_google_maps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path
):
    """Registry rows have no website/reviews. Places runs after scoring, only on the shortlist;
    then website and review rules apply to the businesses that were looked up."""
    from zaheendata.models import SourceKind
    from zaheendata.sources.csv_import import CsvImportSource

    monkeypatch.setenv("ZAHEEN_GOOGLE_MAPS_API_KEY", "k")
    get_settings.cache_clear()
    settings = get_settings()
    db = Database(settings)
    await db.init()
    transport = _PlacesFixtureTransport(fixtures_dir)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "cache.sqlite", 60), transport=transport)
    csv_path = tmp_path / "registry.csv"
    csv_path.write_text(REGISTRY_CSV)
    ctx = SourceContext(http=http, settings=settings)
    spec = CampaignSpec(
        name="m",
        subsector_key="plumbing_hvac",
        geography=GeographySpec(locations=["Austin, TX"]),
        sources=["google_places"],
        contact_enrichment=False,
    )
    campaign = await db.create_campaign(spec)
    source = CsvImportSource(ctx, csv_path, "registry:test", kind=SourceKind.REGISTRY)
    result = await CampaignRunner(db, http, settings, extra_sources=[source]).run(campaign)
    await http.aclose()

    assert result.state == CampaignState.COMPLETED
    assert result.stats["filter_rejections"] == {"franchise/chain": 1}
    assert result.stats["maps_stage"] == "ok"
    assert result.stats["maps_pool"] == 2  # shortlist only; franchise already dropped
    assert result.stats["maps_matched"] == 1
    assert "website_filter_skipped" not in result.stats
    # collect must not have searched Places (those queries look like "plumber in Austin, TX")
    assert not any(" in " in q.lower() for q in transport.text_queries)
    # one lookup per deferred business, named after the registry record
    lookups = sorted(q.upper() for q in transport.text_queries if " IN " not in q.upper())
    assert lookups == [
        "HILL COUNTRY PLUMBING AND AIR LLC AUSTIN, TX",
        "LONE STAR MECHANICAL SERVICES LLC AUSTIN, TX",
    ]
    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert [b.name for b in selected] == ["Hill Country Plumbing & Air"]  # Maps display name wins
    hill = selected[0]
    assert hill.google_place_id == "ChIJ_hill_country" and hill.review_count == 212
    assert hill.domain == "hillcountryplumbingair.com" and hill.employee_count == 14
    assert hill.owner.name == "Marcus Reyes"
    assert {p.source for p in hill.provenance} == {"registry:test", "google_places"}
    rejected = await db.list_businesses(result.id, status=BusinessStatus.REJECTED)
    lone = next(b for b in rejected if b.name.startswith("LONE STAR"))
    assert lone.rejection_reason == "no website" and lone.attributes["maps_match"] == "none"
    await db.close()


async def test_places_is_not_called_until_maps_stage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixtures_dir: Path
):
    """A Google key does not trigger Places during collect/enrich. Stop after score for a free pass."""
    from zaheendata.models import SourceKind
    from zaheendata.sources.csv_import import CsvImportSource

    monkeypatch.setenv("ZAHEEN_GOOGLE_MAPS_API_KEY", "k")
    get_settings.cache_clear()
    settings = get_settings()
    db = Database(settings)
    await db.init()
    transport = _PlacesFixtureTransport(fixtures_dir)
    http = HttpClient(settings, cache=ResponseCache(tmp_path / "cache.sqlite", 60), transport=transport)
    csv_path = tmp_path / "registry.csv"
    csv_path.write_text(REGISTRY_CSV)
    ctx = SourceContext(http=http, settings=settings)
    spec = CampaignSpec(
        name="free",
        subsector_key="plumbing_hvac",
        geography=GeographySpec(locations=["Austin, TX"]),
        sources=["google_places"],
        contact_enrichment=False,
    )
    campaign = await db.create_campaign(spec)
    source = CsvImportSource(ctx, csv_path, "registry:test", kind=SourceKind.REGISTRY)
    result = await CampaignRunner(db, http, settings, extra_sources=[source]).run(
        campaign, stop_after="score"
    )
    await http.aclose()
    assert result.stats.get("maps_stage") is None
    assert transport.text_queries == []
    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert {b.name for b in selected} == {
        "HILL COUNTRY PLUMBING AND AIR LLC",
        "LONE STAR MECHANICAL SERVICES LLC",
    }
    await db.close()
    get_settings.cache_clear()


async def test_beauty_salon_campaign_uses_subsector_rule(env):
    db, result, _ = await _run(env, subsector="beauty_salons")
    selected = await db.list_businesses(result.id, status=BusinessStatus.SELECTED)
    assert len(selected) == 1 and selected[0].name == "Luxe Lane Salon"
    s = selected[0].score.by_name()
    assert s["subsector_signal"] == 2  # 5+ chairs, retail products, no online booking
    assert s["admin_pain"] >= 1  # "never got a call back" + phone-only booking


async def test_api_flow(fixtures_dir: Path):
    transport = httpx.ASGITransport(app=api_mod.app)
    async with (
        api_mod.lifespan(api_mod.app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        assert (await client.get("/health")).json()["status"] == "ok"
        subs = (await client.get("/subsectors")).json()
        assert len(subs) == 17
        sources = (await client.get("/sources")).json()
        assert any(s["name"] == "google_places" for s in sources)
        bad = await client.post(
            "/campaigns", json={"name": "x", "subsector_key": "nope", "geography": {"state": "TX"}}
        )
        assert bad.status_code == 422
        created = await client.post(
            "/campaigns",
            json={
                "name": "x",
                "subsector_key": "roofing",
                "geography": {"locations": ["Austin, TX"]},
                "sources": [],
            },
        )
        assert created.status_code == 201
        cid = created.json()["id"]
        got = await client.get(f"/campaigns/{cid}")
        assert got.json()["state"] == "created" and got.json()["counts"] == {}
        queued = await client.post(f"/campaigns/{cid}/queue")
        assert queued.json()["state"] == "queued"
        export = await client.get(f"/campaigns/{cid}/export")
        assert export.status_code == 200 and export.text.startswith("name,")
        sup = await client.post("/suppressions", json={"kind": "email", "values": ["a@b.com", "a@b.com"]})
        assert sup.json() == {"added": 1}


async def test_api_token_required(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ZAHEEN_API_TOKEN", "secret")
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=api_mod.app)
    async with (
        api_mod.lifespan(api_mod.app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as client,
    ):
        assert (await client.get("/subsectors")).status_code == 401
        ok = await client.get("/subsectors", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
