from pathlib import Path

from zaheendata.geo import default_harvest_layout, parquet_stem, resolve_bbox, state_from_geo


def test_us_and_state_bboxes() -> None:
    west, south, east, north = resolve_bbox("US")
    assert west < -100 and east > -80
    tx = resolve_bbox("TX")
    assert tx[0] < -100
    austin = resolve_bbox("Austin,TX")
    assert austin[1] > 30 and austin[3] < 31


def test_state_from_geo() -> None:
    assert state_from_geo("US") is None
    assert state_from_geo("tx") == "TX"
    assert state_from_geo("Austin,TX") == "TX"


def test_parquet_stem() -> None:
    assert parquet_stem("Austin,TX", "plumbing_hvac") == "overture_plumbing_hvac_Austin_TX"
    assert parquet_stem("GA", None) == "overture_all_GA"


def test_georgia_bbox_is_statewide() -> None:
    west, south, east, north = resolve_bbox("GA")
    assert west < -85 and east > -81
    atlanta = resolve_bbox("Atlanta,GA")
    assert west < atlanta[0] < atlanta[2] < east
    assert south < atlanta[1] < atlanta[3] < north


def test_georgia_harvest_layout_is_isolated() -> None:
    layout = default_harvest_layout("GA")
    assert layout["warehouse"] == Path("data/GA/warehouse.duckdb")
    assert layout["raw_dir"] == Path("data/GA/raw")
    assert layout["out_dir"] == Path("data/GA/exports")
    assert layout["state"] == Path("data/GA/harvest_state.json")
    assert layout["log"] == Path("data/GA/harvest.log")


def test_texas_keeps_legacy_folder_when_warehouse_exists(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "warehouse.duckdb").write_bytes(b"tx")
    tx = default_harvest_layout("TX")
    ga = default_harvest_layout("GA")
    assert tx["warehouse"] == Path("data/warehouse.duckdb")
    assert ga["warehouse"] == Path("data/GA/warehouse.duckdb")
    assert ga["warehouse"] != tx["warehouse"]


def test_georgia_has_no_free_soda_license_feed() -> None:
    from zaheendata.sources.licenses_catalog import list_feeds, soda_source_keys

    assert soda_source_keys("GA") == set()
    keys = {feed.key for feed in list_feeds(state="GA")}
    assert "ga_cilb" in keys
    assert "ga_cosmetology" in keys
