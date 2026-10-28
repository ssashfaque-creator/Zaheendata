"""SBA Paycheck Protection Program FOIA data as a free long-list source.

The SBA published every PPP loan (≈11.5M rows) with ``BorrowerName``, address, ``NAICSCode``,
``JobsReported`` (the only free, nationwide employee-count field), ``FranchiseName`` and
``BusinessType``. That is exactly the NAICS + employee-size + geography filter the brief asks a
list vendor for. Files: https://data.sba.gov/dataset/ppp-foia (``public_150k_plus_*.csv`` and
``public_up_to_150k_*.csv``). Download them with ``zaheen datasets ppp-download`` or drop them in
``ZAHEEN_PPP_DATA_DIR``.

The raw files are large, so the first use builds a per-state index under ``<ppp_data_dir>/_by_state``
(one pass over every CSV); searches then read a single state file.
"""

from __future__ import annotations

import asyncio
import csv
import re
from collections.abc import AsyncIterator, Iterable, Iterator
from pathlib import Path
from typing import Any, ClassVar

from ..logging import get_logger
from ..models import Address, RawRecord, SourceKind, SourceSearchSpec
from ..normalize import normalize_name
from ..settings import Settings
from .base import Source

log = get_logger(__name__)

PPP_SEARCH_URL = "https://data.sba.gov/api/1/search"
PPP_SEARCH_QUERY = "Paycheck Protection Program (PPP) FOIA"


def ppp_distributions(search_response: dict[str, Any]) -> list[dict[str, str]]:
    """Pull the ``public_*.csv`` download URLs out of a data.sba.gov (DKAN) search response."""
    out: list[dict[str, str]] = []
    results = search_response.get("results") or {}
    datasets = results.values() if isinstance(results, dict) else results
    for ds in datasets:
        if "paycheck protection" not in str(ds.get("title", "")).lower():
            continue
        for dist in ds.get("distribution") or []:
            title = str(dist.get("title") or "")
            url = dist.get("downloadURL")
            if url and str(dist.get("format", "")).lower() == "csv" and title.lower().startswith("public_"):
                out.append({"title": title, "url": url})
    return out


INDEX_COLUMNS = [
    "LoanNumber",
    "DateApproved",
    "BorrowerName",
    "BorrowerAddress",
    "BorrowerCity",
    "BorrowerState",
    "BorrowerZip",
    "FranchiseName",
    "JobsReported",
    "NAICSCode",
    "BusinessType",
    "BusinessAgeDescription",
    "NonProfit",
    "InitialApprovalAmount",
    "ForgivenessAmount",
    "ProcessingMethod",
]
INDIVIDUAL_TYPES = {"sole proprietorship", "self-employed individuals", "independent contractors"}
_STATE_RE = re.compile(r"^[A-Z]{2}$")


def _to_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip())) if value not in (None, "") else None
    except ValueError:
        return None


def iter_ppp_csv(path: Path) -> Iterator[dict[str, str]]:
    with path.open(newline="", encoding="utf-8", errors="replace") as fh:
        yield from csv.DictReader(fh)


def build_state_index(csv_files: Iterable[Path], index_dir: Path) -> dict[str, int]:
    """Split the raw PPP CSVs into one slim file per borrower state. Returns rows written per state."""
    index_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, Any] = {}
    writers: dict[str, csv.DictWriter[str]] = {}
    counts: dict[str, int] = {}
    try:
        for path in csv_files:
            log.info("ppp.indexing", file=path.name)
            for row in iter_ppp_csv(path):
                state = (row.get("BorrowerState") or "").strip().upper()
                if not _STATE_RE.match(state):
                    continue
                if state not in writers:
                    fh = (index_dir / f"{state}.csv").open("w", newline="", encoding="utf-8")
                    handles[state] = fh
                    writers[state] = csv.DictWriter(fh, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
                    writers[state].writeheader()
                writers[state].writerow({k: row.get(k, "") for k in INDEX_COLUMNS})
                counts[state] = counts.get(state, 0) + 1
    finally:
        for fh in handles.values():
            fh.close()
    (index_dir / "_complete").write_text("\n".join(f"{s},{n}" for s, n in sorted(counts.items())))
    return counts


def row_to_record(row: dict[str, str], naics_hint: str | None = None) -> RawRecord | None:
    name = (row.get("BorrowerName") or "").strip()
    if not name:
        return None
    jobs = _to_int(row.get("JobsReported"))
    franchise = (row.get("FranchiseName") or "").strip()
    btype = (row.get("BusinessType") or "").strip()
    return RawRecord(
        source="sba_ppp",
        source_kind=SourceKind.REGISTRY,
        source_id=(row.get("LoanNumber") or "").strip() or None,
        name=name.title() if name.isupper() else name,
        address=Address(
            line1=(row.get("BorrowerAddress") or "").strip() or None,
            city=((row.get("BorrowerCity") or "").strip() or None),
            region=(row.get("BorrowerState") or "").strip().upper() or None,
            postal_code=((row.get("BorrowerZip") or "").strip()[:5] or None),
            country="US",
        ),
        naics=(row.get("NAICSCode") or "").strip() or naics_hint,
        employee_count=jobs,
        attributes={
            "franchise_flag": franchise or None,
            "business_type": btype or None,
            "business_age": (row.get("BusinessAgeDescription") or "").strip() or None,
            "individual_structure": btype.lower() in INDIVIDUAL_TYPES,
            "ppp_initial_amount": row.get("InitialApprovalAmount"),
            "ppp_forgiveness_amount": row.get("ForgivenessAmount"),
            "ppp_date_approved": row.get("DateApproved"),
            "ppp_draw": "second" if "second" in (row.get("ProcessingMethod") or "").lower() else "first",
        },
    )


class PppLoansSource(Source):
    name = "sba_ppp"
    kind = SourceKind.REGISTRY
    tags: ClassVar[set[str]] = {"sba_ppp", "employee_count"}
    description = (
        "SBA PPP loan data (FOIA): NAICS, address, JobsReported employee count and franchise name "
        "for ~11M US small businesses. Needs the CSVs in ZAHEEN_PPP_DATA_DIR."
    )
    trust = 55
    always_on = True

    @classmethod
    def csv_files(cls, settings: Settings) -> list[Path]:
        d = settings.ppp_data_dir
        if not d.exists():
            return []
        return sorted(p for p in d.glob("*.csv") if not p.name.startswith("_"))

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return bool(cls.csv_files(settings)) or (settings.ppp_data_dir / "_by_state" / "_complete").exists()

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        if cls.available(settings):
            return None
        return f"no PPP CSVs in {settings.ppp_data_dir} (run `zaheen datasets ppp-download`)"

    @property
    def index_dir(self) -> Path:
        return self.settings.ppp_data_dir / "_by_state"

    async def ensure_index(self) -> None:
        if (self.index_dir / "_complete").exists():
            return
        files = self.csv_files(self.settings)
        if not files:
            raise FileNotFoundError(f"no PPP CSV files in {self.settings.ppp_data_dir}")
        await asyncio.to_thread(build_state_index, files, self.index_dir)

    def _scan_state(self, spec: SourceSearchSpec, city: str | None) -> list[RawRecord]:
        path = self.index_dir / f"{spec.region}.csv"
        if not path.exists():
            return []
        out: list[RawRecord] = []
        seen: set[str] = set()
        for row in iter_ppp_csv(path):
            if not (row.get("NAICSCode") or "").startswith(spec.naics):
                continue
            if (row.get("NonProfit") or "").strip().upper() == "Y":
                continue
            if city and (row.get("BorrowerCity") or "").strip().lower() != city.lower():
                continue
            jobs = _to_int(row.get("JobsReported"))
            lo = spec.employee_min if spec.employee_min is not None else 0
            hi = spec.employee_max if spec.employee_max is not None else 10**9
            if jobs is None or not (lo <= jobs <= hi):
                continue
            key = f"{normalize_name(row.get('BorrowerName') or '')}|{(row.get('BorrowerZip') or '')[:5]}"
            if key in seen:  # first + second draw loans for the same borrower
                continue
            rec = row_to_record(row, spec.naics)
            if rec is None:
                continue
            seen.add(key)
            out.append(rec)
        return out

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        if not spec.region:
            return
        await self.ensure_index()
        city = None
        if spec.location_text and "," in spec.location_text:
            city = spec.location_text.split(",")[0].strip()
        records = await asyncio.to_thread(self._scan_state, spec, city)
        log.info("ppp.scanned", state=spec.region, city=city, naics=spec.naics, records=len(records))
        for rec in records:
            yield rec
