from __future__ import annotations

import os
from pathlib import Path

import duckdb

from zaheendata.geo import resolve_bbox
from zaheendata.subsectors import SUBSECTORS, Subsector, get_subsector

OVERTURE_RELEASE = os.environ.get("OVERTURE_RELEASE", "2026-10-21.0")
PLACES_GLOB = (
    f"s3://overturemaps-us-west-2/release/{OVERTURE_RELEASE}/theme=places/type=place/*"
)


def _category_sql(subsectors: list[Subsector]) -> str:
    labels = sorted({label for spec in subsectors for label in spec.overture_categories})
    quoted = ", ".join("'" + label.replace("'", "''") + "'" for label in labels)
    return quoted


def download_places(
    *,
    geo: str,
    subsector_key: str | None,
    out: Path,
    limit: int | None = None,
    connection: duckdb.DuckDBPyConnection | None = None,
) -> Path:
    west, south, east, north = resolve_bbox(geo)
    specs = [get_subsector(subsector_key)] if subsector_key else list(SUBSECTORS.values())
    categories = _category_sql(specs)
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    case_sql = " CASE taxonomy.primary "
    for spec in specs:
        for label in spec.overture_categories:
            case_sql += f"WHEN '{label}' THEN '{spec.key}' "
    case_sql += "ELSE '' END AS subsector, "
    naics_sql = " CASE taxonomy.primary "
    for spec in specs:
        for label in spec.overture_categories:
            naics_sql += f"WHEN '{label}' THEN '{spec.naics}' "
    naics_sql += "ELSE '' END AS naics "

    sql = f"""
    COPY (
      SELECT
        id,
        names.primary AS name,
        websites[1] AS website,
        phones[1] AS phone,
        emails[1] AS email,
        cast(socials AS VARCHAR) AS socials,
        basic_category,
        taxonomy.primary AS taxonomy_primary,
        cast(taxonomy.hierarchy AS VARCHAR) AS taxonomy_hierarchy,
        addresses[1].freeform AS address,
        addresses[1].locality AS city,
        addresses[1].region AS region,
        addresses[1].postcode AS postcode,
        addresses[1].country AS country,
        brand.names.primary AS brand,
        confidence,
        operating_status,
        bbox.xmin AS lng,
        bbox.ymin AS lat,
        {case_sql}
        {naics_sql}
      FROM read_parquet('{PLACES_GLOB}', hive_partitioning=1)
      WHERE bbox.xmin BETWEEN {west} AND {east}
        AND bbox.ymin BETWEEN {south} AND {north}
        AND addresses[1].country = 'US'
        AND coalesce(operating_status, 'open') <> 'permanently_closed'
        AND taxonomy.primary IN ({categories})
      {limit_sql}
    ) TO '{out.as_posix()}' (FORMAT PARQUET)
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    owned = connection is None
    con = connection or duckdb.connect()
    try:
        if owned:
            con.execute("INSTALL httpfs; LOAD httpfs;")
            con.execute("SET s3_region='us-west-2';")
        con.execute(sql)
    finally:
        if owned:
            con.close()
    return out


def probe_places(geo: str = "Austin,TX") -> dict:
    west, south, east, north = resolve_bbox(geo)
    sql = f"""
      SELECT names.primary, websites[1], phones[1], taxonomy.primary
      FROM read_parquet('{PLACES_GLOB}', hive_partitioning=1)
      WHERE bbox.xmin BETWEEN {west} AND {east}
        AND bbox.ymin BETWEEN {south} AND {north}
        AND addresses[1].country = 'US'
        AND taxonomy.primary IN ('plumbing', 'hvac_service')
      LIMIT 1
    """
    con = duckdb.connect()
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute("SET s3_region='us-west-2';")
        row = con.execute(sql).fetchone()
    finally:
        con.close()
    if not row:
        return {"ok": False, "geo": geo, "release": OVERTURE_RELEASE}
    return {
        "ok": True,
        "geo": geo,
        "release": OVERTURE_RELEASE,
        "name": row[0],
        "website": row[1],
        "phone": row[2],
        "category": row[3],
    }
