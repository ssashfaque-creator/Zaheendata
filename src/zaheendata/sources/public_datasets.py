"""Government / public datasets connector driven by ``config/public_datasets.yaml``.

Covers three jobs that paid list vendors used to do:

* long list     - NAICS-coded tax-permit and business-licence rolls (Texas sales-tax permits, LA and
                  SF business registrations, CT registry ...) filtered server-side by NAICS prefix
                  or by industry keywords;
* cross-check   - licence boards matched to a sub-sector through tags;
* owner lookup  - Secretary of State registries that publish officer / registered-agent names.
"""

from __future__ import annotations

import csv
import re
import time
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from ..logging import get_logger
from ..models import Address, OwnerContact, RawRecord, SourceKind, SourceSearchSpec
from ..normalize import normalize_name
from ..registry import SubSector, get_registry
from ..settings import get_settings
from .base import Source
from .csv_import import row_to_record

log = get_logger(__name__)


class PublicDataset(BaseModel):
    key: str
    name: str
    kind: str = "registry"  # license | registry | tax_registry | business_license
    type: str  # socrata | csv
    states: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    long_list: bool = False
    owner_lookup: bool = False
    owner_title_default: str | None = None
    url: str
    filters: str | None = None
    page_size: int = 5000
    naics_column: str | None = None
    naics_match: str = "prefix"  # prefix | number | contains
    naics_prefix_length: int | None = None
    dba_column: str | None = None
    owner_name_last_first: bool = False
    state_column: str | None = None
    city_column: str | None = None
    county_column: str | None = None
    keyword_column: str | None = None
    keyword_filter: str | None = None
    keyword_filter_by_tag: dict[str, str] = Field(default_factory=dict)
    keyword_filter_by_query: bool = False
    lookup_name_column: str | None = None
    columns: dict[str, str]

    @property
    def source_kind(self) -> SourceKind:
        return SourceKind.LICENSE if self.kind == "license" else SourceKind.REGISTRY

    def covers(self, region: str | None, city: str | None) -> bool:
        if self.states and region and region not in self.states:
            return False
        return not (self.cities and city and city.lower() not in {c.lower() for c in self.cities})


@lru_cache
def load_public_datasets() -> list[PublicDataset]:
    path = get_settings().config_dir / "public_datasets.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [PublicDataset(**d) for d in data.get("datasets", [])]


def _soql_quote(value: str) -> str:
    return value.replace("'", "''")


def _naics_clause(ds: PublicDataset, naics: str) -> str:
    prefix = naics[: ds.naics_prefix_length] if ds.naics_prefix_length else naics
    if ds.naics_match == "number":
        if len(prefix) >= 6:
            return f"{ds.naics_column} = {int(prefix)}"
        scale = 10 ** (6 - len(prefix))
        return (
            f"{ds.naics_column} >= {int(prefix) * scale} AND {ds.naics_column} < {(int(prefix) + 1) * scale}"
        )
    if ds.naics_match == "contains":
        needle = f"({prefix})" if len(prefix) >= 6 else f"({prefix}"
        return f"{ds.naics_column} like '%{_soql_quote(needle)}%'"
    return f"starts_with({ds.naics_column}, '{_soql_quote(prefix)}')"


def _naics_in_row(ds: PublicDataset, row: dict[str, Any], naics: str) -> bool:
    prefix = naics[: ds.naics_prefix_length] if ds.naics_prefix_length else naics
    value = str(row.get(ds.naics_column or "") or "")
    if ds.naics_match == "contains":
        m = re.search(r"\d{2,6}", value)
        return m is not None and m.group(0).startswith(prefix)
    return value.strip().startswith(prefix)


def _person_name(value: str, last_first: bool) -> str:
    value = value.strip()
    if last_first and "," in value:
        last, first = value.split(",", 1)
        value = f"{first.strip()} {last.strip()}"
    return value.title() if value.isupper() else value


_CITY_STATE_ZIP = re.compile(
    r"^\s*(.+?)[,\s]+([A-Z]{2})\s+(\d{5})(?:-\d{4})?\s*$",
    re.IGNORECASE,
)


def parse_city_state_zip(value: str | None) -> tuple[str | None, str | None, str | None]:
    """Split TDLR-style ``AUSTIN, TX 78701`` into city, state, ZIP."""
    if not value or not str(value).strip():
        return None, None, None
    m = _CITY_STATE_ZIP.match(str(value).strip())
    if not m:
        return None, None, None
    city = m.group(1).strip(" ,").title()
    return (city or None), m.group(2).upper(), m.group(3)


def _apply_city_state_zip(ds: PublicDataset, row: dict[str, Any], rec: RawRecord) -> None:
    col = ds.columns.get("city_state_zip")
    if not col:
        return
    city, region, postal = parse_city_state_zip(str(row.get(col) or "") or None)
    if not (city or region or postal):
        return
    addr = rec.address or Address()
    if city and not addr.city:
        addr.city = city
    if region and not addr.region:
        addr.region = region
    if postal and not addr.postal_code:
        addr.postal_code = postal
    rec.address = addr


def _has_contact(rec: RawRecord) -> bool:
    addr = rec.address
    return bool(rec.phone or rec.website or (addr and (addr.line1 or addr.city or addr.postal_code)))


def socrata_metadata_url(resource_url: str) -> str | None:
    """https://host/resource/abcd-1234.json -> https://host/api/views/abcd-1234.json"""
    m = re.match(r"^(https?://[^/]+)/resource/([a-z0-9]{4}-[a-z0-9]{4})\.json", resource_url)
    return f"{m.group(1)}/api/views/{m.group(2)}.json" if m else None


def _regex_to_like(column: str, pattern: str) -> str | None:
    """Turn a simple alternation regex ('electric|plumb') into a SoQL OR of upper() LIKE clauses.
    Alternatives containing regex metacharacters are skipped (client-side regex still applies)."""
    clauses = []
    for alt in pattern.split("|"):
        alt = alt.strip()
        if not alt or re.search(r"[\\^$.*+?()\[\]{}]", alt):
            continue
        clauses.append(f"upper({column}) like '%{_soql_quote(alt.upper())}%'")
    return "(" + " OR ".join(clauses) + ")" if clauses else None


class PublicDatasetSource(Source):
    name = "public_datasets"
    kind = SourceKind.REGISTRY
    tags: ClassVar[set[str]] = {
        "state_contractor_license",
        "state_electrical_license",
        "state_cosmetology_license",
        "state_pest_control_license",
        "state_business_registration",
        "public_datasets",
    }
    description = (
        "Government open data: NAICS-coded tax/business registrations, licence boards, Secretary of State "
        "registries (Socrata / CSV)."
    )
    trust = 60
    always_on = True

    def applies_to(self, subsector: SubSector) -> bool:
        wanted = set(subsector.cross_check)
        return any(ds.long_list or (wanted & set(ds.tags)) for ds in load_public_datasets())

    # ------------------------------------------------------------------ selection
    def _plan(
        self, subsector: SubSector, spec: SourceSearchSpec
    ) -> list[tuple[PublicDataset, str | None, list[str]]]:
        """Return (dataset, matched_tag, keyword_patterns) for every dataset relevant to this search."""
        city = (spec.location_text or "").split(",")[0].strip() if spec.location_text else None
        out = []
        for ds in load_public_datasets():
            if not ds.covers(spec.region, city):
                continue
            tag = next((t for t in ds.tags if t in subsector.cross_check), None)
            if tag is None and not ds.long_list:
                continue
            patterns: list[str] = []
            if tag and ds.keyword_filter_by_tag.get(tag):
                patterns.append(ds.keyword_filter_by_tag[tag])
            elif ds.keyword_filter:
                patterns.append(ds.keyword_filter)
            if ds.keyword_filter_by_query and not ds.naics_column:
                terms = list(dict.fromkeys([*subsector.keywords, *spec.queries]))
                patterns.append("|".join(re.escape(q) for q in terms))
            if tag is None and not ds.naics_column and not patterns:
                continue  # nothing to narrow a generic registry down to the sub-sector
            out.append((ds, tag, patterns))
        return out

    # ------------------------------------------------------------------ fetching
    def _where(self, ds: PublicDataset, spec: SourceSearchSpec, patterns: list[str]) -> str | None:
        city = (spec.location_text or "").split(",")[0].strip() if spec.location_text else ""
        parts: list[str] = []
        if ds.filters:
            parts.append("(" + ds.filters.format(state=spec.region or "", city=city) + ")")
        if ds.naics_column:
            parts.append(_naics_clause(ds, spec.naics))
        if ds.state_column and spec.region:
            parts.append(f"upper({ds.state_column}) = '{spec.region.upper()}'")
        metro = bool(spec.location_text and "," in spec.location_text)
        if ds.city_column and city and metro:
            parts.append(f"upper({ds.city_column}) = '{_soql_quote(city.upper())}'")
        elif ds.county_column and spec.county and metro:
            parts.append(f"upper({ds.county_column}) like '{_soql_quote(spec.county.upper())}%'")
        if ds.keyword_column:
            for p in patterns:
                like = _regex_to_like(ds.keyword_column, p)
                if like:
                    parts.append(like)
        return " AND ".join(parts) if parts else None

    async def _iter_socrata(
        self, ds: PublicDataset, where: str | None, max_rows: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        offset = 0
        fetched = 0
        while True:
            remaining = None if max_rows is None else max_rows - fetched
            if remaining is not None and remaining <= 0:
                return
            page = ds.page_size if remaining is None else min(ds.page_size, remaining)
            params: dict[str, Any] = {"$limit": page, "$offset": offset}
            if where:
                params["$where"] = where
            rows = await self.http.get_json(ds.url, params=params, meter="socrata.query")
            for row in rows:
                yield row
            fetched += len(rows)
            if len(rows) < page:
                break
            offset += page

    async def _csv_path(self, ds: PublicDataset) -> Path:
        path = self.settings.data_dir / "datasets" / f"{ds.key}.csv"
        refresh = self.settings.public_dataset_refresh_days * 24 * 3600
        if path.exists() and time.time() - path.stat().st_mtime < refresh:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        await self.http.download(ds.url, path)
        return path

    async def _iter_csv(self, ds: PublicDataset) -> AsyncIterator[dict[str, Any]]:
        path = await self._csv_path(ds)
        with path.open(newline="", encoding="utf-8", errors="replace") as fh:
            for row in csv.DictReader(fh):
                yield row

    @staticmethod
    def _row_matches(
        ds: PublicDataset,
        row: dict[str, Any],
        spec: SourceSearchSpec,
        patterns: list[str],
        regexes: list[re.Pattern[str]],
        city: str | None,
    ) -> bool:
        if ds.naics_column and ds.type == "csv" and not _naics_in_row(ds, row, spec.naics):
            return False
        if regexes:
            hay = (
                str(row.get(ds.keyword_column) or "")
                if ds.keyword_column
                else " ".join(str(v) for v in row.values() if v)
            )
            if not all(rx.search(hay) for rx in regexes):
                return False
        if city and ds.type == "csv" and ds.columns.get("city"):
            row_city = str(row.get(ds.columns["city"]) or "").strip().lower()
            if row_city and row_city != city.lower():
                return False
        return True

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        subsector = get_registry().get(spec.subsector_key)
        city = (
            (spec.location_text or "").split(",")[0].strip()
            if spec.location_text and "," in spec.location_text
            else None
        )
        for ds, tag, patterns in self._plan(subsector, spec):
            regexes = [re.compile(p, re.IGNORECASE) for p in patterns]
            count = 0
            skipped_bare = 0
            # Never let one licence roll dump thousands of name-only rows into a 200-company test.
            cap = max(50, spec.limit)
            try:
                rows = (
                    self._iter_socrata(ds, self._where(ds, spec, patterns), max_rows=cap * 3)
                    if ds.type == "socrata"
                    else self._iter_csv(ds)
                )
                async for row in rows:
                    if not self._row_matches(ds, row, spec, patterns, regexes, city):
                        continue
                    rec = row_to_record(row, ds.columns, f"{ds.kind}:{ds.key}", spec.naics, ds.source_kind)
                    if rec is None:
                        continue
                    _apply_city_state_zip(ds, row, rec)
                    dba = str(row.get(ds.dba_column) or "").strip() if ds.dba_column else ""
                    if dba and dba.lower() != rec.name.lower():
                        rec.attributes["legal_name"] = rec.name
                        rec.name = dba
                    if rec.owner_name:
                        rec.owner_name = _person_name(rec.owner_name, ds.owner_name_last_first)
                        if ds.owner_title_default and not rec.owner_title:
                            rec.owner_title = ds.owner_title_default
                    rec.attributes["dataset"] = ds.name
                    rec.attributes["dataset_role"] = "cross_check" if tag else "long_list"
                    if ds.kind == "license" and not _has_contact(rec):
                        skipped_bare += 1
                        continue
                    count += 1
                    yield rec
                    if count >= cap:
                        log.info("public_dataset.capped", dataset=ds.key, cap=cap)
                        break
            except Exception as exc:  # noqa: BLE001 - a broken dataset must not stop the campaign
                log.warning("public_dataset.failed", dataset=ds.key, error=str(exc)[:300])
            log.info("public_dataset.done", dataset=ds.key, records=count, skipped_bare=skipped_bare)

    # ------------------------------------------------------------------ owner lookup
    async def lookup_owner(self, business_name: str, region: str | None) -> OwnerContact | None:
        target = normalize_name(business_name)
        if not target or not region:
            return None
        first_token = target.split()[0].upper()
        for ds in load_public_datasets():
            if not (
                ds.owner_lookup and ds.type == "socrata" and ds.lookup_name_column and region in ds.states
            ):
                continue
            owner_cols = [
                ds.columns.get(k) for k in ("owner_name", "owner_first", "owner_last") if ds.columns.get(k)
            ]
            if not owner_cols:
                continue
            where = f"upper({ds.lookup_name_column}) like '{_soql_quote(first_token)}%'"
            if ds.filters:
                where = f"({ds.filters}) AND {where}"
            try:
                rows = await self.http.get_json(
                    ds.url, params={"$where": where, "$limit": 50}, meter="socrata.query"
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("registry.lookup_failed", dataset=ds.key, error=str(exc)[:200])
                continue
            best, best_score = None, 0.0
            for row in rows:
                score = fuzz.token_sort_ratio(
                    target, normalize_name(str(row.get(ds.lookup_name_column) or ""))
                )
                if score > best_score:
                    best, best_score = row, score
            if best is None or best_score < 88:
                continue
            name = best.get(ds.columns.get("owner_name", "")) or " ".join(
                filter(
                    None,
                    [best.get(ds.columns.get("owner_first", "")), best.get(ds.columns.get("owner_last", ""))],
                )
            )
            if not name:
                continue
            title = (
                best.get(ds.columns.get("owner_title", ""))
                or ds.owner_title_default
                or "Officer (state registry)"
            )
            return OwnerContact(
                name=_person_name(str(name), ds.owner_name_last_first),
                title=str(title),
                source=f"registry:{ds.key}",
                confidence=0.4
                if (ds.owner_title_default or "").lower().startswith("registered agent")
                else 0.55,
            )
        return None
