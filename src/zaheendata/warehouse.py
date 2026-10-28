from __future__ import annotations

from pathlib import Path

import duckdb

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS places (
    id TEXT PRIMARY KEY,
    name TEXT,
    name_key TEXT,
    phone TEXT,
    phone_key TEXT,
    website TEXT,
    domain TEXT,
    email TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    country TEXT,
    lat DOUBLE,
    lng DOUBLE,
    basic_category TEXT,
    taxonomy_primary TEXT,
    taxonomy_hierarchy TEXT,
    brand TEXT,
    socials TEXT,
    confidence DOUBLE,
    operating_status TEXT,
    subsector TEXT,
    naics TEXT,
    source TEXT,
    own_website BOOLEAN
);

CREATE TABLE IF NOT EXISTS licenses (
    source TEXT,
    business_name TEXT,
    name_key TEXT,
    owner_name TEXT,
    owner_is_person BOOLEAN,
    license_number TEXT,
    license_type TEXT,
    license_status TEXT,
    phone TEXT,
    phone_key TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    county TEXT
);

CREATE TABLE IF NOT EXISTS ppp (
    borrower_name TEXT,
    name_key TEXT,
    address TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    jobs_reported INTEGER,
    naics TEXT,
    franchise_name TEXT,
    phone TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
    domain TEXT,
    url TEXT,
    source TEXT,
    fetched_at TIMESTAMP,
    status INTEGER,
    bytes INTEGER,
    cache_path TEXT
);

CREATE TABLE IF NOT EXISTS technologies (
    domain TEXT,
    tool TEXT,
    category TEXT,
    evidence TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS leads (
    business_id TEXT PRIMARY KEY,
    name TEXT,
    phone TEXT,
    website TEXT,
    website_crawled BOOLEAN,
    address TEXT,
    city TEXT,
    state TEXT,
    postal_code TEXT,
    owner_name TEXT,
    owner_email TEXT,
    owner_phone TEXT,
    employee_count TEXT,
    naics TEXT,
    score_total INTEGER,
    rank INTEGER,
    status TEXT,
    sources TEXT,
    lat DOUBLE,
    lng DOUBLE,
    domain TEXT,
    google_place_id TEXT,
    categories TEXT,
    owner_title TEXT,
    owner_linkedin TEXT,
    owner_source TEXT,
    owner_confidence DOUBLE,
    email_is_role_account BOOLEAN,
    suppressed BOOLEAN,
    suppression_reasons TEXT,
    rejection_reason TEXT,
    license_number TEXT,
    license_status TEXT,
    online_booking BOOLEAN,
    booking_provider TEXT,
    quote_form BOOLEAN,
    says_call_for_quote BOOLEAN,
    emergency_service BOOLEAN,
    maintenance_plan BOOLEAN,
    recurring_service BOOLEAN,
    free_estimate BOOLEAN,
    online_store BOOLEAN,
    store_platform TEXT,
    sells_gift_cards BOOLEAN,
    hosts_events BOOLEAN,
    instagram TEXT,
    tech_stack TEXT,
    tool_categories TEXT,
    pain_mentions TEXT,
    pain_examples TEXT,
    google_rating DOUBLE,
    google_reviews INTEGER,
    recent_reviews_90d INTEGER,
    last_review_at TEXT,
    busy_and_growing INTEGER,
    admin_pain INTEGER,
    multi_tool INTEGER,
    owner_reachable INTEGER,
    subsector_signal INTEGER,
    score_evidence TEXT,
    subsector TEXT
);

CREATE INDEX IF NOT EXISTS places_name_key ON places(name_key);
CREATE INDEX IF NOT EXISTS places_domain ON places(domain);
CREATE INDEX IF NOT EXISTS places_subsector ON places(subsector);
CREATE INDEX IF NOT EXISTS technologies_domain ON technologies(domain);
CREATE INDEX IF NOT EXISTS licenses_name_key ON licenses(name_key);
"""


def connect(path: Path | None = None, *, remote: bool = False) -> duckdb.DuckDBPyConnection:
    target = path or Path("data/warehouse.duckdb")
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target))
    if remote:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        try:
            con.execute("INSTALL spatial; LOAD spatial;")
        except duckdb.Error:
            pass
        con.execute("SET s3_region='us-west-2';")
    con.execute(SCHEMA_SQL)
    for column, typ in (
        ("facebook", "TEXT"),
        ("youtube", "TEXT"),
        ("founded_year", "TEXT"),
        ("family_owned", "BOOLEAN"),
        ("financing", "TEXT"),
        ("licensed_bonded_insured", "BOOLEAN"),
        ("extra_phones", "TEXT"),
        ("brand", "TEXT"),
        ("license_type", "TEXT"),
    ):
        try:
            con.execute(f"ALTER TABLE leads ADD COLUMN {column} {typ}")
        except duckdb.Error:
            pass
    return con


def warehouse_stats(path: Path | None = None) -> dict:
    con = connect(path)
    def _count(table: str) -> int:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])

    by_subsector = dict(
        con.execute(
            "SELECT coalesce(subsector, ''), count(*) FROM places GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
    )
    tools = dict(
        con.execute(
            "SELECT tool, count(*) FROM technologies GROUP BY 1 ORDER BY 2 DESC LIMIT 40"
        ).fetchall()
    )
    own = int(con.execute("SELECT count(*) FROM places WHERE own_website").fetchone()[0])
    stats = {
        "warehouse": str(path or Path("data/warehouse.duckdb")),
        "places": _count("places"),
        "places_own_website": own,
        "licenses": _count("licenses"),
        "ppp": _count("ppp"),
        "snapshots": _count("snapshots"),
        "technologies": _count("technologies"),
        "leads": _count("leads"),
        "by_subsector": by_subsector,
        "tools": tools,
    }
    con.close()
    return stats
