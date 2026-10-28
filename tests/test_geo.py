from zaheendata.geo import parquet_stem, resolve_bbox, state_from_geo


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
