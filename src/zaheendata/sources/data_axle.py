"""Data Axle Platform API connector (Places + People packages).

Filters by primary NAICS, employee count and geography exactly like the brief describes. Field
names follow the Data Axle "places" schema; adjust ``ATTRIBUTES`` if your contract uses a
different package."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from ..models import Address, GeoPoint, RawRecord, SourceKind, SourceSearchSpec
from ..settings import Settings
from .base import Source

SEARCH_URL = "https://api.data-axle.com/v1/places/search"
ATTRIBUTES = {
    "naics": "primary_naics_code",
    "employees": "location_employee_count",
    "state": "state",
    "city": "city",
}


class DataAxleSource(Source):
    name = "data_axle"
    kind = SourceKind.VENDOR
    tags: ClassVar[set[str]] = {"data_axle", "vendor"}
    description = "Data Axle Platform API: NAICS + employee-size + geography filtered business records."
    trust = 75
    always_on = True

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return settings.data_axle_api_key is not None

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "set ZAHEEN_DATA_AXLE_API_KEY"

    def _filter(self, spec: SourceSearchSpec) -> dict[str, Any]:
        clauses: list[dict[str, Any]] = [
            {"relation": "begins_with", "attribute": ATTRIBUTES["naics"], "value": spec.naics},
            {
                "relation": "between",
                "attribute": ATTRIBUTES["employees"],
                "value": [
                    spec.employee_min or self.settings.employee_min,
                    spec.employee_max or self.settings.employee_max,
                ],
            },
        ]
        if spec.region:
            clauses.append({"relation": "equals", "attribute": ATTRIBUTES["state"], "value": spec.region})
        if spec.location_text and "," in spec.location_text:
            city = spec.location_text.split(",")[0].strip()
            clauses.append({"relation": "equals", "attribute": ATTRIBUTES["city"], "value": city})
        return {"relation": "and", "value": clauses}

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        headers = {
            "X-AUTH-TOKEN": self.settings.secret("data_axle_api_key") or "",
            "Content-Type": "application/json",
        }
        offset = 0
        page_size = 100
        while offset < max(spec.limit, page_size) * 3:
            body: dict[str, Any] = {
                "filter": self._filter(spec),
                "packages": ["standard_v1", "enhanced_v2"],
                "limit": page_size,
                "offset": offset,
            }
            if spec.center:
                body["geo_point"] = {"lat": spec.center.lat, "lon": spec.center.lng}
                body["radius"] = f"{spec.radius_km}km"
            data = await self.http.post_json(
                SEARCH_URL, json_body=body, headers=headers, meter="data_axle.search"
            )
            docs = data.get("documents") or []
            for d in docs:
                contacts = d.get("contacts") or []
                owner = next(
                    (
                        c
                        for c in contacts
                        if any(k in (c.get("title") or "").lower() for k in ("owner", "president", "ceo"))
                    ),
                    contacts[0] if contacts else None,
                )
                yield RawRecord(
                    source=self.name,
                    source_kind=self.kind,
                    source_id=d.get("infogroup_id") or d.get("id"),
                    name=d.get("name") or "",
                    phone=d.get("phone"),
                    website=d.get("website"),
                    address=Address(
                        line1=d.get("street"),
                        city=d.get("city"),
                        region=d.get("state"),
                        postal_code=d.get("postal_code"),
                        country="US",
                    ),
                    location=GeoPoint(lat=d["latitude"], lng=d["longitude"]) if d.get("latitude") else None,
                    naics=str(d.get(ATTRIBUTES["naics"]) or spec.naics),
                    employee_count=d.get(ATTRIBUTES["employees"]) or d.get("employee_count"),
                    owner_name=" ".join(filter(None, [owner.get("first_name"), owner.get("last_name")]))
                    if owner
                    else None,
                    owner_title=owner.get("title") if owner else None,
                    attributes={
                        "sic_code": d.get("primary_sic_code_id"),
                        "sales_volume": d.get("location_sales_volume"),
                        "year_established": d.get("year_established"),
                        "franchise_flag": d.get("franchise_description") or d.get("is_franchise"),
                        "chain_flag": d.get("chain_flag") or d.get("is_chain"),
                    },
                )
            if len(docs) < page_size:
                break
            offset += page_size
