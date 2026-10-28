"""State licence-list connector driven by ``config/license_sources.yaml`` (Socrata or CSV)."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, ClassVar

import yaml
from pydantic import BaseModel, Field

from ..logging import get_logger
from ..models import RawRecord, SourceKind, SourceSearchSpec
from ..registry import SubSector, get_registry
from ..settings import get_settings
from .base import Source
from .csv_import import row_to_record

log = get_logger(__name__)


class LicenseDataset(BaseModel):
    key: str
    name: str
    type: str  # socrata | csv
    states: list[str]
    tags: list[str]
    url: str
    filters: str | None = None
    page_size: int = 5000
    columns: dict[str, str]
    keyword_filter: str | None = None
    keyword_filter_by_tag: dict[str, str] = Field(default_factory=dict)


@lru_cache
def load_license_datasets() -> list[LicenseDataset]:
    path = get_settings().config_dir / "license_sources.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [LicenseDataset(**d) for d in data.get("datasets", [])]


class StateLicenseSource(Source):
    name = "state_license"
    kind = SourceKind.LICENSE
    tags: ClassVar[set[str]] = {
        "state_contractor_license",
        "state_electrical_license",
        "state_cosmetology_license",
        "state_pest_control_license",
        "state_business_registration",
    }
    description = "State licence rolls (contractor, electrical, cosmetology, pest control) via Socrata/CSV."
    trust = 55

    def applies_to(self, subsector: SubSector) -> bool:
        wanted = self.tags & set(subsector.cross_check)
        return any(wanted & set(ds.tags) for ds in load_license_datasets())

    def _datasets_for(self, subsector: SubSector, region: str | None) -> list[tuple[LicenseDataset, str]]:
        out = []
        for ds in load_license_datasets():
            if region and region not in ds.states:
                continue
            for tag in ds.tags:
                if tag in subsector.cross_check:
                    out.append((ds, tag))
                    break
        return out

    async def _iter_rows(self, ds: LicenseDataset, spec: SourceSearchSpec) -> AsyncIterator[dict[str, Any]]:
        if ds.type == "socrata":
            offset = 0
            while True:
                params: dict[str, Any] = {"$limit": ds.page_size, "$offset": offset}
                if ds.filters:
                    params["$where"] = ds.filters.format(
                        state=spec.region or "", city=(spec.location_text or "").split(",")[0]
                    )
                rows = await self.http.get_json(ds.url, params=params)
                for row in rows:
                    yield row
                if len(rows) < ds.page_size:
                    break
                offset += ds.page_size
        else:
            resp = await self.http.get(ds.url, max_bytes=200_000_000)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.content.decode("utf-8", errors="replace")))
            for row in reader:
                yield row

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        subsector = get_registry().get(spec.subsector_key)
        city = (spec.location_text or "").split(",")[0].strip().lower() if spec.location_text else None
        for ds, tag in self._datasets_for(subsector, spec.region):
            pattern = ds.keyword_filter_by_tag.get(tag) or ds.keyword_filter
            regex = re.compile(pattern, re.IGNORECASE) if pattern else None
            count = 0
            try:
                async for row in self._iter_rows(ds, spec):
                    if regex:
                        hay = " ".join(str(v) for v in row.values() if v)
                        if not regex.search(hay):
                            continue
                    if city and ds.columns.get("city"):
                        row_city = str(row.get(ds.columns["city"]) or "").strip().lower()
                        if row_city and row_city != city:
                            continue
                    rec = row_to_record(row, ds.columns, f"license:{ds.key}", spec.naics, SourceKind.LICENSE)
                    if rec:
                        rec.attributes["license_dataset"] = ds.name
                        count += 1
                        yield rec
            except Exception as exc:  # noqa: BLE001 - one broken dataset must not stop the campaign
                log.warning("license.dataset_failed", dataset=ds.key, error=str(exc))
            log.info("license.dataset_done", dataset=ds.key, records=count)
