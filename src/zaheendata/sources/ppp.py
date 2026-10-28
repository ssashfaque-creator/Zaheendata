from __future__ import annotations

import csv
import io
from pathlib import Path

import httpx

from zaheendata.normalize import name_key, normalize_phone

PACKAGE_URL = "https://data.sba.gov/api/3/action/package_show?id=ppp-foia"
RESOURCE_STEMS = (
    "public_150k_plus_240930.csv",
    "public_up_to_150k_1_240930.csv",
    "public_up_to_150k_2_240930.csv",
    "public_up_to_150k_3_240930.csv",
    "public_up_to_150k_4_240930.csv",
    "public_up_to_150k_5_240930.csv",
    "public_up_to_150k_6_240930.csv",
    "public_up_to_150k_7_240930.csv",
    "public_up_to_150k_8_240930.csv",
    "public_up_to_150k_9_240930.csv",
    "public_up_to_150k_10_240930.csv",
    "public_up_to_150k_11_240930.csv",
    "public_up_to_150k_12_240930.csv",
)


def resource_urls(client: httpx.Client | None = None) -> list[str]:
    owned = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=60.0)
    try:
        response = client.get(PACKAGE_URL)
        response.raise_for_status()
        resources = response.json()["result"]["resources"]
        wanted = {stem.lower() for stem in RESOURCE_STEMS}
        urls = []
        for item in resources:
            name = (item.get("name") or item.get("url") or "").lower()
            if any(stem in name for stem in wanted) and item.get("url"):
                urls.append(item["url"])
        return urls
    finally:
        if owned:
            client.close()


def stream_filtered_rows(
    url: str,
    *,
    naics: str | None = None,
    state: str | None = None,
    client: httpx.Client | None = None,
) -> list[dict[str, str]]:
    owned = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=120.0)
    kept: list[dict[str, str]] = []
    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            text_stream = io.TextIOWrapper(response.iter_raw(), encoding="utf-8", errors="replace")
            reader = csv.DictReader(text_stream)
            for row in reader:
                code = (row.get("NAICSCode") or row.get("NAICS") or "").strip()
                st = (row.get("BorrowerState") or "").strip().upper()
                if naics and not code.startswith(naics):
                    continue
                if state and st != state.upper():
                    continue
                jobs = row.get("JobsReported") or ""
                kept.append(
                    {
                        "borrower_name": row.get("BorrowerName") or "",
                        "name_key": name_key(row.get("BorrowerName")),
                        "address": row.get("BorrowerAddress") or "",
                        "city": row.get("BorrowerCity") or "",
                        "state": st,
                        "postal_code": (row.get("BorrowerZip") or "")[:10],
                        "jobs_reported": jobs,
                        "naics": code,
                        "franchise_name": row.get("FranchiseName") or "",
                        "phone": normalize_phone(row.get("BorrowerPhone") or ""),
                    }
                )
    finally:
        if owned:
            client.close()
    return kept


def download_ppp(
    *,
    out: Path,
    naics: str | None = None,
    state: str | None = None,
    plus_only: bool = True,
) -> Path:
    urls = resource_urls()
    if plus_only:
        urls = [url for url in urls if "150k_plus" in url.lower()] or urls[:1]
    rows: list[dict[str, str]] = []
    with httpx.Client(follow_redirects=True, timeout=120.0) as client:
        for url in urls:
            rows.extend(stream_filtered_rows(url, naics=naics, state=state, client=client))
    out.parent.mkdir(parents=True, exist_ok=True)
    csv_path = out.with_suffix(".csv")
    fieldnames = [
        "borrower_name",
        "name_key",
        "address",
        "city",
        "state",
        "postal_code",
        "jobs_reported",
        "naics",
        "franchise_name",
        "phone",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    if out.suffix == ".parquet":
        import duckdb

        con = duckdb.connect()
        try:
            con.execute(
                f"COPY (SELECT * FROM read_csv_auto('{csv_path.as_posix()}')) "
                f"TO '{out.as_posix()}' (FORMAT PARQUET)"
            )
        finally:
            con.close()
    return out
