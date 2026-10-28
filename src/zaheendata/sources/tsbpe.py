from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Any, Iterable

import httpx

from zaheendata.cache import JsonCache
from zaheendata.normalize import looks_like_person, person_display_name

# TSBPE publishes a daily Responsible Master Plumber CSV. The public page is
# Cloudflare-protected, so the pipeline prefers a local file drop.
DEFAULT_URLS = (
    "https://tsbpe.texas.gov/wp-content/uploads/RMP.csv",
    "https://www.tsbpe.texas.gov/docs/RMP.csv",
)


def load_rmp_rows(
    *,
    path: Path | None = None,
    url: str | None = None,
    client: httpx.Client | None = None,
    cache: JsonCache | None = None,
) -> list[dict[str, str]]:
    if path:
        text = path.read_text(encoding="utf-8", errors="replace")
    else:
        cache_key = url or "default"
        if cache:
            cached = cache.get("tsbpe", cache_key)
            if cached is not None:
                return cached
        text = _download(url, client)
    rows = list(csv.DictReader(io.StringIO(text)))
    if cache and not path:
        cache.set("tsbpe", url or "default", rows)
    return rows


def _download(url: str | None, client: httpx.Client | None) -> str:
    owned = client is None
    client = client or httpx.Client(follow_redirects=True, headers={"User-Agent": "Zaheendata/0.1"})
    errors: list[str] = []
    try:
        for candidate in ((url,) if url else DEFAULT_URLS):
            if not candidate:
                continue
            try:
                response = client.get(candidate, timeout=60.0)
                if response.status_code == 200 and "," in response.text[:400]:
                    return response.text
                errors.append(f"{candidate} -> HTTP {response.status_code}")
            except httpx.HTTPError as exc:
                errors.append(f"{candidate} -> {exc}")
    finally:
        if owned:
            client.close()
    raise FileNotFoundError(
        "Could not download the TSBPE Responsible Master Plumber list. "
        "Save the CSV from https://tsbpe.texas.gov/free-licensee-list/ and "
        "pass --tsbpe-file. Attempts: " + "; ".join(errors)
    )


def iter_match_records(rows: Iterable[dict[str, str]], city: str | None = None) -> Iterable[dict[str, Any]]:
    city_key = (city or "").strip().lower()
    for row in rows:
        lowered = {key.lower().replace(" ", "_"): value for key, value in row.items()}
        business = (
            lowered.get("company")
            or lowered.get("company_name")
            or lowered.get("business_name")
            or lowered.get("org_name")
            or ""
        ).strip()
        owner = (
            lowered.get("licensee")
            or lowered.get("name")
            or lowered.get("rmp_name")
            or f"{lowered.get('first_name', '')} {lowered.get('last_name', '')}".strip()
        ).strip()
        city_value = (lowered.get("city") or lowered.get("business_city") or "").strip()
        if city_key and city_value and city_key not in city_value.lower():
            continue
        yield {
            "source": "tsbpe_rmp",
            "business_name": business or owner,
            "owner_name": person_display_name(owner) if looks_like_person(owner) else owner,
            "owner_is_person": looks_like_person(owner),
            "license_number": (
                lowered.get("license")
                or lowered.get("license_number")
                or lowered.get("lic_no")
                or ""
            ).strip(),
            "license_type": "Responsible Master Plumber",
            "license_status": (lowered.get("status") or lowered.get("ins_expiry_dte") or "").strip(),
            "phone": (lowered.get("phone") or lowered.get("telephone") or "").strip(),
            "address": (lowered.get("address") or lowered.get("street") or "").strip(),
            "city_state_zip": " ".join(
                part for part in (city_value, lowered.get("state"), lowered.get("zip") or lowered.get("zipcode")) if part
            ),
            "county": (lowered.get("county") or "").strip(),
        }
