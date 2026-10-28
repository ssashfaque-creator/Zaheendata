from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from zaheendata import api as api_mod
from zaheendata.export import COLUMNS, flatten, to_csv, to_xlsx
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


async def test_end_to_end_pipeline(env):
    db, result, events = await _run(env)
    assert result.state == CampaignState.COMPLETED
    assert [e for e in events if "." not in e] == [
        "collect",
        "resolve",
        "filter",
        "enrich",
        "score",
        "contacts",
        "finalize",
    ]
    stats = result.stats
    assert stats["raw_records"] == 12 and stats["businesses"] == 9
    assert stats["filter_rejections"] == {
        "franchise/chain": 1,
        "outside target area": 1,
        "no website": 1,
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
    assert [b.name for b in scored] == ["Barton Springs Plumbing"]
    rejected = await db.list_businesses(result.id, status=BusinessStatus.REJECTED)
    assert {b.name: b.rejection_reason.split(":")[0] for b in rejected} == {
        "Roto-Rooter Plumbing & Water Cleanup": "franchise/chain",
        "Jose's Plumbing Service": "no website",
        "Capital City Drain Pros": "too few reviews",
        "Big D Heating & Air": "outside target area",
        "Old Town Plumbing": "closed",
    }
    counts = await db.business_counts(result.id)
    assert counts == {"selected": 3, "scored": 1, "rejected": 5}


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
    assert len(csv_text.splitlines()) == 4
    assert to_xlsx(items, sup)[:2] == b"PK"


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
        assert export.status_code == 200 and export.text.startswith("rank,")
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
