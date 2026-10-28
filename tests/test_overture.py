from zaheendata.sources.overture import PLACES_GLOB, _category_sql, download_places
from zaheendata.subsectors import get_subsector


def test_category_sql_includes_plumbing() -> None:
    sql = _category_sql([get_subsector("plumbing_hvac")])
    assert "'plumbing'" in sql
    assert "'hvac_service'" in sql


def test_download_sql_targets_public_s3(tmp_path) -> None:
    # Compile the COPY statement without hitting the network.
    from zaheendata.geo import resolve_bbox
    from zaheendata.sources import overture as module

    captured: dict[str, str] = {}

    class Fake:
        def execute(self, sql):
            captured["sql"] = sql

        def close(self):
            return None

    out = tmp_path / "places.parquet"
    west, south, east, north = resolve_bbox("Austin,TX")
    original = module.duckdb.connect

    def boom():
        return Fake()

    module.duckdb.connect = lambda: Fake()  # type: ignore[method-assign]
    try:
        download_places(geo="Austin,TX", subsector_key="plumbing_hvac", out=out, connection=Fake())
    finally:
        module.duckdb.connect = original  # type: ignore[method-assign]

    sql = captured["sql"]
    assert PLACES_GLOB in sql
    assert "taxonomy.primary IN" in sql
    assert "plumbing" in sql
    assert str(west) in sql
    assert "COPY" in sql
    assert out.as_posix() in sql
    assert "addresses[1].country = 'US'" in sql
