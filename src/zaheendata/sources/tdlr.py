from __future__ import annotations

import os
from typing import Any, Iterable

import httpx

from zaheendata.cache import JsonCache
from zaheendata.normalize import looks_like_person, person_display_name

SODA_URL = "https://data.texas.gov/resource/7358-krk7.json"

LICENSE_FILTERS = {
    "tdlr_ac_contractor": "upper(license_type)='A/C CONTRACTOR'",
    "tdlr_electrical_contractor": "upper(license_type)='ELECTRICAL CONTRACTOR'",
    "tdlr_cosmetology_establishment": (
        "upper(license_type) like '%ESTABLISHMENT%' "
        "and upper(license_type) not like '%SCHOOL%'"
    ),
}


def soda_headers() -> dict[str, str]:
    token = os.environ.get("SOCRATA_APP_TOKEN", "").strip()
    return {"X-App-Token": token} if token else {}


def fetch_licenses(
    *,
    license_filter: str,
    county: str | None,
    client: httpx.Client | None = None,
    cache: JsonCache | None = None,
    page_size: int = 1000,
    max_rows: int = 20000,
) -> list[dict[str, Any]]:
    where = license_filter
    if county:
        where = f"{where} AND upper(business_county)='{county.upper()}'"
    cache_key = f"{where}|{page_size}|{max_rows}"
    if cache:
        cached = cache.get("tdlr", cache_key)
        if cached is not None:
            return cached

    owned = client is None
    client = client or httpx.Client()
    rows: list[dict[str, Any]] = []
    try:
        offset = 0
        while offset < max_rows:
            response = client.get(
                SODA_URL,
                params={
                    "$where": where,
                    "$limit": min(page_size, max_rows - offset),
                    "$offset": offset,
                    "$order": "license_number",
                },
                headers=soda_headers(),
                timeout=60.0,
            )
            response.raise_for_status()
            batch = response.json()
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < page_size:
                break
            offset += len(batch)
    finally:
        if owned:
            client.close()
    if cache:
        cache.set("tdlr", cache_key, rows)
    return rows


def iter_match_records(rows: Iterable[dict[str, Any]], source_label: str) -> Iterable[dict[str, Any]]:
    for row in rows:
        owner = (row.get("owner_name") or "").strip()
        business = (row.get("business_name") or "").strip()
        yield {
            "source": source_label,
            "business_name": business,
            "owner_name": person_display_name(owner) if looks_like_person(owner) else owner,
            "owner_is_person": looks_like_person(owner),
            "license_number": (row.get("license_number") or "").strip(),
            "license_type": (row.get("license_type") or "").strip(),
            "license_subtype": (row.get("license_subtype") or "").strip(),
            "license_status": "active"
            if (row.get("license_expiration_date_mmddccyy") or "")
            else "",
            "phone": (row.get("business_telephone") or row.get("owner_telephone") or "").strip(),
            "address": (row.get("business_address_line1") or "").strip(),
            "city_state_zip": (row.get("business_city_state_zip") or "").strip(),
            "county": (row.get("business_county") or "").strip(),
        }
