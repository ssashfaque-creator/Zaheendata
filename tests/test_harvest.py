from pathlib import Path

import duckdb

from zaheendata.checkpoint import HarvestState, json_records_ready
from zaheendata.models import empty_lead
from zaheendata.pipeline.enrich import enrich_live
from zaheendata.pipeline.harvest import harvest, ordered_subsectors
from zaheendata.pipeline.ingest import attach_warehouse_overlays, hydrate_from_warehouse, ingest_license_json
from zaheendata.pipeline.build import persist_leads
from zaheendata.sources.overture import parquet_file_ready
from zaheendata.warehouse import connect, reset_warehouse, snapshot_domains


def _write_multi_parquet(path: Path) -> None:
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
          SELECT
            'place-hvac' AS id,
            'Cool Air HVAC' AS name,
            'https://coolair.example' AS website,
            '+15125550100' AS phone,
            'jane@coolair.example' AS email,
            '' AS socials,
            'hvac_service' AS basic_category,
            'hvac_service' AS taxonomy_primary,
            'hvac' AS taxonomy_hierarchy,
            '100 Main' AS address,
            'Austin' AS city,
            'TX' AS region,
            '78701' AS postcode,
            'US' AS country,
            NULL AS brand,
            0.9 AS confidence,
            'open' AS operating_status,
            -97.74 AS lng,
            30.27 AS lat,
            'plumbing_hvac' AS subsector,
            '238220' AS naics
          UNION ALL
          SELECT
            'place-elec', 'Spark Electric', 'https://spark.example',
            '+15125550111', '', '', 'electrical', 'electrical', 'electrical',
            '200 Main', 'Austin', 'TX', '78702', 'US', NULL, 0.8, 'open',
            -97.73, 30.28, 'electrical', '238210'
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
        """
    )
    con.close()


def test_ordered_subsectors_puts_licensed_trades_first() -> None:
    keys = ordered_subsectors()
    assert keys[:4] == ["plumbing_hvac", "electrical", "beauty_salons", "nail_salons"]
    assert len(keys) == 17


def test_harvest_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "harvest_state.json"
    state = HarvestState.load(path)
    state.data["geo"] = "TX"
    state.set_phase("crawl:electrical")
    row = state.subsector("electrical")
    row["crawled"] = 12
    state.save()
    loaded = HarvestState.load(path)
    assert loaded.data["geo"] == "TX"
    assert loaded.data["phase"] == "crawl:electrical"
    assert loaded.subsector("electrical")["crawled"] == 12


def test_parquet_and_json_ready_helpers(tmp_path: Path) -> None:
    missing = tmp_path / "nope.parquet"
    assert parquet_file_ready(missing) is False
    parquet = tmp_path / "ok.parquet"
    _write_multi_parquet(parquet)
    assert parquet_file_ready(parquet) is True
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    assert json_records_ready(empty) is False
    filled = tmp_path / "licenses.json"
    filled.write_text('[{"source":"tx_tdlr_ac"}]', encoding="utf-8")
    assert json_records_ready(filled) is True


def test_license_sources_do_not_cross_trades(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse.duckdb"
    licenses = tmp_path / "licenses.json"
    licenses.write_text(
        "["
        '{"source":"tx_tdlr_electrical","business_name":"Cool Air HVAC",'
        '"owner_name":"Wrong Electrician","owner_is_person":true,'
        '"license_number":"TECL999","license_status":"active",'
        '"phone":"","address":"","city_state_zip":"Austin TX","county":"TRAVIS"},'
        '{"source":"tx_tdlr_ac","business_name":"Cool Air HVAC",'
        '"owner_name":"Jane Plumber","owner_is_person":true,'
        '"license_number":"TACLA000","license_status":"active",'
        '"phone":"","address":"","city_state_zip":"Austin TX","county":"TRAVIS"}'
        "]",
        encoding="utf-8",
    )
    ingest_license_json(licenses, warehouse, state="TX")
    hvac = empty_lead(name="Cool Air HVAC", website="https://coolair.example/")
    attach_warehouse_overlays([hvac], warehouse, allowed_sources={"tx_tdlr_ac"})
    assert hvac.license_number == "TACLA000"
    assert hvac.owner_name == "Jane Plumber"

    crossed = empty_lead(name="Cool Air HVAC", website="https://coolair.example/")
    attach_warehouse_overlays([crossed], warehouse, allowed_sources={"tx_tdlr_electrical"})
    assert crossed.license_number == "TECL999"


def test_hydrate_restores_crawled_fields(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse.duckdb"
    saved = empty_lead(
        name="Cool Air HVAC",
        business_id="place-hvac",
        domain="coolair.example",
        website="https://coolair.example/",
        website_crawled=True,
        owner_email="jane@coolair.example",
        tech_stack="jobber,housecall_pro",
        founded_year="2004",
    )
    persist_leads([saved], warehouse)
    fresh = empty_lead(
        name="Cool Air HVAC",
        business_id="place-hvac",
        domain="coolair.example",
        website="https://coolair.example/",
    )
    hydrate_from_warehouse([fresh], warehouse)
    assert fresh.website_crawled is True
    assert fresh.owner_email == "jane@coolair.example"
    assert "jobber" in fresh.tech_stack
    assert fresh.founded_year == "2004"


def test_enrich_live_skips_domains_already_in_snapshots(tmp_path: Path, monkeypatch) -> None:
    warehouse = tmp_path / "warehouse.duckdb"
    con = connect(warehouse)
    con.execute(
        "INSERT INTO snapshots VALUES ('coolair.example', 'https://coolair.example/', 'live_crawl', now(), 200, 12, '')"
    )
    con.close()
    seen: list[list[str]] = []

    async def fake_crawl(leads, **_kwargs):
        seen.append([lead.domain for lead in leads])
        return leads

    monkeypatch.setattr("zaheendata.pipeline.enrich.crawl_leads", fake_crawl)
    lead = empty_lead(
        name="Cool Air HVAC",
        domain="coolair.example",
        website="https://coolair.example/",
    )
    enrich_live([lead], warehouse=warehouse, concurrency=1)
    assert seen == [[]]
    assert lead.website_crawled is True
    assert "coolair.example" in snapshot_domains(warehouse, successful_only=True)


def _seed_raw(raw: Path) -> None:
    raw.mkdir(parents=True, exist_ok=True)
    _write_multi_parquet(raw / "overture_all_TX.parquet")
    for key in ("tx_tdlr_ac", "tx_tdlr_electrical", "tx_tdlr_salon"):
        (raw / f"{key}.json").write_text(
            '[{"source":"%s","business_name":"Cool Air HVAC","owner_name":"Jane Plumber",'
            '"owner_is_person":true,"license_number":"TACLA000","license_status":"active",'
            '"phone":"","address":"","city_state_zip":"Austin TX","county":"TRAVIS"}]' % key,
            encoding="utf-8",
        )


def test_harvest_resumes_completed_trade(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    _seed_raw(raw)
    warehouse = tmp_path / "warehouse.duckdb"
    out_dir = tmp_path / "exports"
    state_path = tmp_path / "harvest_state.json"
    log_path = tmp_path / "harvest.log"

    first = harvest(
        geo="TX",
        subsector_keys=["plumbing_hvac", "electrical"],
        warehouse=warehouse,
        raw_dir=raw,
        out_dir=out_dir,
        state_path=state_path,
        log_path=log_path,
        crawl="none",
    )
    assert first["stopped"] is False
    assert first["trades"]["plumbing_hvac"]["status"] == "complete"
    assert first["trades"]["electrical"]["status"] == "complete"
    assert (out_dir / "TX_plumbing_hvac.csv").exists()
    assert (out_dir / "TX_electrical.csv").exists()

    state = HarvestState.load(state_path)
    assert state.subsector("plumbing_hvac")["status"] == "complete"

    second = harvest(
        geo="TX",
        subsector_keys=["plumbing_hvac", "electrical"],
        warehouse=warehouse,
        raw_dir=raw,
        out_dir=out_dir,
        state_path=state_path,
        log_path=log_path,
        crawl="none",
    )
    assert second["trades"]["plumbing_hvac"]["status"] == "complete"
    log_text = log_path.read_text(encoding="utf-8")
    assert "already complete" in log_text
    assert "reusing" in log_text


def test_harvest_stop_mid_crawl_then_resume(tmp_path: Path, monkeypatch) -> None:
    raw = tmp_path / "raw"
    _seed_raw(raw)
    warehouse = tmp_path / "warehouse.duckdb"
    out_dir = tmp_path / "exports"
    state_path = tmp_path / "harvest_state.json"
    log_path = tmp_path / "harvest.log"
    calls = {"n": 0}

    def fake_enrich(leads, *, should_stop=None, on_progress=None, warehouse=None, **_kwargs):
        calls["n"] += 1
        if leads:
            lead = leads[0]
            lead.website_crawled = True
            lead.extra["crawl_attempted"] = True
            lead.extra["_html"] = "<script src='https://cdn.shopify.com/s/files/x.js'></script>"
            if on_progress:
                on_progress(lead)
            from zaheendata.pipeline.enrich import checkpoint_live_lead

            checkpoint_live_lead(lead, warehouse)
        if calls["n"] == 1 and should_stop is not None:
            should_stop.request_stop()
        return leads

    monkeypatch.setattr("zaheendata.pipeline.harvest.enrich_live", fake_enrich)
    paused = harvest(
        geo="TX",
        subsector_keys=["plumbing_hvac", "electrical"],
        warehouse=warehouse,
        raw_dir=raw,
        out_dir=out_dir,
        state_path=state_path,
        log_path=log_path,
        crawl="live",
    )
    assert paused["stopped"] is True
    assert "electrical" not in paused["trades"]
    assert paused["trades"]["plumbing_hvac"]["status"] == "paused"
    assert HarvestState.load(state_path).subsector("plumbing_hvac")["status"] == "paused"

    resumed = harvest(
        geo="TX",
        subsector_keys=["plumbing_hvac", "electrical"],
        warehouse=warehouse,
        raw_dir=raw,
        out_dir=out_dir,
        state_path=state_path,
        log_path=log_path,
        crawl="live",
    )
    assert resumed["stopped"] is False
    assert resumed["trades"]["electrical"]["status"] == "complete"
    assert calls["n"] >= 2


def test_reset_warehouse_deletes_db(tmp_path: Path) -> None:
    warehouse = tmp_path / "warehouse.duckdb"
    connect(warehouse).close()
    assert warehouse.exists()
    reset_warehouse(warehouse)
    assert not warehouse.exists()
