from __future__ import annotations

import os
from typing import Any

import httpx

CBP_URL = "https://api.census.gov/data/2023/cbp"

# Travis County, TX
COUNTY_FIPS = {
    ("austin", "tx"): ("48", "453"),
}


def establishment_counts(
    *,
    naics: str,
    city: str,
    state: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not key:
        return {}
    fips = COUNTY_FIPS.get((city.lower(), state.lower()))
    if not fips:
        return {}
    owned = client is None
    client = client or httpx.Client()
    try:
        response = client.get(
            CBP_URL,
            params={
                "get": "ESTAB,EMPSZES,EMPSZES_LABEL,NAME",
                "for": f"county:{fips[1]}",
                "in": f"state:{fips[0]}",
                "NAICS2017": naics,
                "key": key,
            },
            timeout=30.0,
        )
        if response.status_code != 200:
            return {}
        table = response.json()
    except httpx.HTTPError:
        return {}
    finally:
        if owned:
            client.close()
    if not table or len(table) < 2:
        return {}
    header, *rows = table
    out: dict[str, Any] = {}
    for row in rows:
        record = dict(zip(header, row))
        out[record.get("EMPSZES_LABEL") or record.get("EMPSZES") or ""] = record.get("ESTAB")
    return out
