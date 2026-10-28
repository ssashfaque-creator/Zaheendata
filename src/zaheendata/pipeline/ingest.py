from __future__ import annotations

import json
from pathlib import Path

from zaheendata.franchises import franchise_hit
from zaheendata.models import Lead, empty_lead
from zaheendata.normalize import (
    DIRECTORY_HOSTS,
    canonicalize_website,
    domain_from_url,
    email_is_role_account,
    is_own_website,
    is_usable_email,
    distinctive_name_key,
    looks_like_person,
    name_key,
    normalize_phone,
    parse_socials,
    person_display_name,
    phone_key,
)
from zaheendata.warehouse import connect


def _directory_sql(column: str = "website") -> str:
    clauses = []
    for host in sorted(DIRECTORY_HOSTS):
        safe = host.replace("'", "''")
        clauses.append(f"{column} ILIKE '%{safe}%'")
    return " OR ".join(clauses)


def ingest_overture_parquet(path: Path, warehouse: Path | None = None) -> int:
    con = connect(warehouse)
    con.execute("CREATE OR REPLACE TEMP TABLE incoming AS SELECT * FROM read_parquet(?)", [path.as_posix()])
    directory = _directory_sql("website")
    con.execute(
        f"""
        INSERT OR REPLACE INTO places
        SELECT
            id,
            name,
            regexp_replace(lower(coalesce(name, '')), '[^a-z0-9]+', '', 'g') AS name_key,
            phone,
            regexp_replace(coalesce(phone, ''), '\\D', '', 'g') AS phone_key,
            website,
            lower(regexp_replace(regexp_replace(coalesce(website, ''), '^https?://(www\\.)?', ''), '/.*', '')) AS domain,
            email,
            address,
            city,
            region AS state,
            postcode AS postal_code,
            country,
            lat,
            lng,
            basic_category,
            taxonomy_primary,
            taxonomy_hierarchy,
            brand,
            socials,
            confidence,
            operating_status,
            subsector,
            naics,
            'overture' AS source,
            CASE
              WHEN website IS NULL OR website = '' THEN FALSE
              WHEN {directory} THEN FALSE
              ELSE TRUE
            END AS own_website
        FROM incoming
        """
    )
    count = con.execute("SELECT count(*) FROM incoming").fetchone()[0]
    con.close()
    return int(count)


def ingest_license_json(path: Path, warehouse: Path | None = None, state: str = "") -> int:
    records = json.loads(path.read_text(encoding="utf-8"))
    con = connect(warehouse)
    sources = {record.get("source") for record in records if record.get("source")}
    for source in sources:
        con.execute("DELETE FROM licenses WHERE source = ?", [source])
    rows = [
        [
            record.get("source"),
            record.get("business_name"),
            name_key(record.get("business_name")),
            record.get("owner_name"),
            bool(record.get("owner_is_person")),
            record.get("license_number"),
            record.get("license_type"),
            record.get("license_status"),
            record.get("phone"),
            phone_key(record.get("phone")),
            record.get("address"),
            record.get("city_state_zip"),
            state,
            record.get("county"),
        ]
        for record in records
    ]
    if rows:
        con.executemany("INSERT INTO licenses VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.close()
    return len(records)


def ingest_ppp_file(path: Path, warehouse: Path | None = None) -> int:
    con = connect(warehouse)
    reader = "read_parquet" if path.suffix == ".parquet" else "read_csv_auto"
    con.execute(
        f"""
        INSERT INTO ppp
        SELECT
            borrower_name, name_key, address, city, state, postal_code,
            try_cast(jobs_reported AS INTEGER), naics, franchise_name, phone
        FROM {reader}(?)
        """,
        [path.as_posix()],
    )
    count = con.execute(f"SELECT count(*) FROM {reader}(?)", [path.as_posix()]).fetchone()[0]
    con.close()
    return int(count)


def places_to_leads(
    warehouse: Path | None = None,
    subsector: str | None = None,
    *,
    own_website_only: bool = True,
    limit: int | None = None,
) -> list[Lead]:
    con = connect(warehouse)
    where = "WHERE 1=1"
    params: list = []
    if subsector:
        where += " AND subsector = ?"
        params.append(subsector)
    if own_website_only:
        where += " AND own_website = TRUE"
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = con.execute(
        f"""
        SELECT id, name, phone, website, email, address, city, state, postal_code,
               lat, lng, domain, subsector, naics, taxonomy_primary, socials,
               own_website, source, confidence, brand
        FROM places
        {where}
        {limit_sql}
        """,
        params,
    ).fetchall()
    columns = [item[0] for item in con.description]
    con.close()
    leads: list[Lead] = []
    for row in rows:
        data = dict(zip(columns, row))
        website = canonicalize_website(data.get("website") or "")
        if website and not is_own_website(website):
            website = ""
        parsed = parse_socials(str(data.get("socials") or ""))
        lead = empty_lead(
            name=data.get("name") or "",
            phone=normalize_phone(data.get("phone")),
            website=website,
            owner_email=(data.get("email") or "") if is_usable_email(data.get("email") or "") else "",
            email_is_role_account=email_is_role_account(data.get("email") or ""),
            address=data.get("address") or "",
            city=data.get("city") or "",
            state=data.get("state") or "",
            postal_code=str(data.get("postal_code") or "")[:10],
            lat=data.get("lat") or "",
            lng=data.get("lng") or "",
            domain=domain_from_url(website) or (data.get("domain") or ""),
            naics=data.get("naics") or "",
            categories=data.get("taxonomy_primary") or "",
            business_id=data.get("id") or "",
            instagram=parsed.get("instagram", ""),
            facebook=parsed.get("facebook", ""),
            youtube=parsed.get("youtube", ""),
            owner_linkedin=parsed.get("owner_linkedin", ""),
            brand=data.get("brand") or "",
            status="discovered",
        )
        lead.merge_source("overture")
        lead.extra["subsector"] = data.get("subsector")
        lead.extra["confidence"] = data.get("confidence")
        leads.append(lead)
    return leads


def attach_warehouse_overlays(leads: list[Lead], warehouse: Path | None = None) -> list[Lead]:
    con = connect(warehouse)
    licenses = con.execute(
        "SELECT business_name, owner_name, owner_is_person, license_number, license_status, phone, name_key, source, license_type FROM licenses"
    ).fetchall()
    ppp_rows = con.execute(
        "SELECT borrower_name, jobs_reported, franchise_name, name_key, state FROM ppp"
    ).fetchall()
    con.close()
    lic_by_key = {row[6]: row for row in licenses if row[6]}
    lic_by_phone = {phone_key(row[5]): row for row in licenses if row[5]}
    lic_by_distinct: dict[str, list] = {}
    for row in licenses:
        distinct = distinctive_name_key(row[0])
        if distinct:
            lic_by_distinct.setdefault(distinct, []).append(row)
    ppp_by_key = {row[3]: row for row in ppp_rows if row[3]}

    def _license_for(lead: Lead):
        key = name_key(lead.name)
        hit = lic_by_key.get(key)
        if hit:
            return hit
        phone = phone_key(lead.phone)
        if phone and phone in lic_by_phone:
            return lic_by_phone[phone]
        distinct = distinctive_name_key(lead.name)
        candidates = lic_by_distinct.get(distinct, []) if distinct else []
        if len(candidates) == 1:
            return candidates[0]
        return None

    for lead in leads:
        lic = _license_for(lead)
        if lic:
            lead.merge_source(lic[7] or "license")
            lead.license_number = lic[3] or lead.license_number
            lead.license_status = lic[4] or lead.license_status
            lead.license_type = (lic[8] if len(lic) > 8 else "") or lead.license_type
            owner = person_display_name(lic[1]) if lic[1] else ""
            if looks_like_person(owner) or looks_like_person(lic[1]):
                lead.owner_name = owner or lead.owner_name
                lead.owner_source = lic[7] or "license"
                lead.owner_confidence = 0.8
                lead.owner_title = lead.owner_title or "Owner / license holder"
                if lic[5] and not lead.owner_phone:
                    lead.owner_phone = normalize_phone(lic[5]) or lead.owner_phone
            if lic[5] and not lead.phone:
                lead.phone = normalize_phone(lic[5]) or lead.phone
        ppp = ppp_by_key.get(name_key(lead.name))
        if ppp:
            lead.merge_source("sba_ppp")
            if ppp[1] is not None:
                lead.employee_count = str(ppp[1])
            if ppp[2]:
                hit = franchise_hit(ppp[2]) or ppp[2]
                if hit and not lead.rejection_reason:
                    lead.extra["ppp_franchise"] = hit
    return leads


def dedupe_leads(leads: list[Lead]) -> list[Lead]:
    kept: dict[str, Lead] = {}
    leftover: list[Lead] = []
    for lead in leads:
        key = (lead.domain or "").lower() or f"name:{name_key(lead.name)}|{lead.city.lower()}"
        existing = kept.get(key)
        if existing is None:
            kept[key] = lead
            continue
        if looks_like_person(lead.owner_name) and not looks_like_person(existing.owner_name):
            existing.owner_name = lead.owner_name
            existing.owner_source = lead.owner_source
            existing.owner_confidence = lead.owner_confidence
        if lead.owner_email and not existing.owner_email:
            existing.owner_email = lead.owner_email
        if lead.license_number and not existing.license_number:
            existing.license_number = lead.license_number
            existing.license_status = lead.license_status
            existing.license_type = lead.license_type
        if lead.tech_stack and not existing.tech_stack:
            existing.tech_stack = lead.tech_stack
            existing.tool_categories = lead.tool_categories
        for source in (lead.sources or "").split(";"):
            existing.merge_source(source.strip())
        leftover.append(lead)
    return list(kept.values())
