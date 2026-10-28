from pathlib import Path

import duckdb

from zaheendata.models import empty_lead
from zaheendata.pipeline.build import LEAD_COLUMNS, LEAD_INSERT_SQL, _lead_values, persist_leads
from zaheendata.pipeline.filter import keep_for_enrichment
from zaheendata.pipeline.ingest import attach_warehouse_overlays, ingest_license_json, ingest_overture_parquet, places_to_leads
from zaheendata.pipeline.score import score_lead
from zaheendata.subsectors import get_subsector
from zaheendata.warehouse import connect, warehouse_stats


def _write_places_parquet(path: Path) -> None:
    con = duckdb.connect()
    con.execute(
        f"""
        COPY (
          SELECT
            'place-1' AS id,
            'Cool Air HVAC' AS name,
            'https://coolair.example' AS website,
            '+15125550100' AS phone,
            'jane@coolair.example' AS email,
            'https://instagram.com/coolairatx' AS socials,
            'hvac_service' AS basic_category,
            'hvac_service' AS taxonomy_primary,
            'hvac' AS taxonomy_hierarchy,
            '100 Main St' AS address,
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
            'place-2', 'Mr Rooter of Austin', 'https://mrrooter.com',
            '', '', '', 'plumbing', 'plumbing', 'plumbing',
            '', 'Austin', 'TX', '78702', 'US', 'Mr Rooter', 0.8, 'open',
            -97.73, 30.28, 'plumbing_hvac', '238220'
          UNION ALL
          SELECT
            'place-3', 'Yelp Only Shop', 'https://www.yelp.com/biz/x',
            '', '', '', 'plumbing', 'plumbing', 'plumbing',
            '', 'Austin', 'TX', '78703', 'US', NULL, 0.5, 'open',
            -97.72, 30.26, 'plumbing_hvac', '238220'
        ) TO '{path.as_posix()}' (FORMAT PARQUET)
        """
    )
    con.close()


def test_lead_insert_placeholders_match_columns() -> None:
    assert LEAD_INSERT_SQL.count("?") == len(LEAD_COLUMNS) == 61
    assert len(_lead_values(empty_lead(name="Cool Air"))) == len(LEAD_COLUMNS)


def test_ingest_filters_directories_and_overlays(tmp_path: Path) -> None:
    parquet = tmp_path / "places.parquet"
    warehouse = tmp_path / "warehouse.duckdb"
    _write_places_parquet(parquet)
    assert ingest_overture_parquet(parquet, warehouse) == 3

    licenses = tmp_path / "licenses.json"
    licenses.write_text(
        '[{"source":"tdlr:a/c_contractor","business_name":"Cool Air HVAC",'
        '"owner_name":"Jane Plumber","owner_is_person":true,'
        '"license_number":"TACLA000","license_status":"active",'
        '"phone":"","address":"","city_state_zip":"Austin TX","county":"TRAVIS"}]',
        encoding="utf-8",
    )
    ingest_license_json(licenses, warehouse, state="TX")

    leads = places_to_leads(warehouse, subsector="plumbing_hvac", own_website_only=False)
    assert len(leads) == 3
    own = places_to_leads(warehouse, subsector="plumbing_hvac", own_website_only=True)
    assert [lead.name for lead in own] == ["Cool Air HVAC", "Mr Rooter of Austin"]

    attach_warehouse_overlays(own, warehouse)
    cool = next(lead for lead in own if lead.name == "Cool Air HVAC")
    assert cool.owner_name == "Jane Plumber"
    assert cool.license_number == "TACLA000"
    assert cool.owner_email == "jane@coolair.example"

    kept = keep_for_enrichment(own, get_subsector("plumbing_hvac"), require_reviews=False)
    assert [lead.name for lead in kept] == ["Cool Air HVAC"]

    cool.tech_stack = "shopify,quickbooks,jobber"
    cool.tool_categories = "ecommerce,accounting,field_service"
    cool.emergency_service = True
    cool.maintenance_plan = True
    score_lead(cool, get_subsector("plumbing_hvac"))
    persist_leads([cool], warehouse)
    stats = warehouse_stats(warehouse)
    assert stats["places"] == 3
    assert stats["places_own_website"] == 2
    assert stats["licenses"] == 1
    assert stats["leads"] == 1
    assert stats["by_subsector"]["plumbing_hvac"] == 3
