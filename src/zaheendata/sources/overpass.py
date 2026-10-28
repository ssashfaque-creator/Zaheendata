"""OpenStreetMap (Overpass API) connector: free, open-licensed cross-check source.

Coverage is thinner than Google but it is useful to catch independents vendors miss and it
carries ``brand`` tags that flag chains. ODbL attribution applies; see docs/DATA_SOURCES.md."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from ..logging import get_logger
from ..models import Address, GeoPoint, RawRecord, SourceKind, SourceSearchSpec
from ..registry import get_registry
from ..settings import Settings
from .base import Source

log = get_logger(__name__)


class OverpassSource(Source):
    name = "osm_overpass"
    kind = SourceKind.MAPS
    tags: ClassVar[set[str]] = {"osm"}
    description = "OpenStreetMap POIs via Overpass (name, phone, website, address, brand tags)."
    trust = 30
    always_on = True

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return settings.enable_overpass

    def _build_query(self, osm_tags: list[str], lat: float, lng: float, radius_m: int) -> str:
        clauses = []
        for tag in osm_tags:
            key, _, value = tag.partition("=")
            clauses.append(f'nwr["{key}"="{value}"](around:{radius_m},{lat},{lng});')
        return f"[out:json][timeout:12];({''.join(clauses)});out center tags 500;"

    async def _query_with_failover(self, query: str) -> dict:
        """Public Overpass instances rate-limit and block aggressively; try mirrors in turn."""
        urls = [self.settings.overpass_url, *self.settings.overpass_fallback_urls]
        last_error: Exception | None = None
        for url in urls:
            host = url.split("/")[2]
            self.http.set_host_rate(host, 0.2, concurrency=1)
            try:
                return await self.http.post_json(url, data={"data": query}, retries=1, timeout_seconds=15.0)
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                last_error = exc
                log.warning(
                    "overpass.mirror_failed",
                    url=url,
                    error=(str(exc) or type(exc).__name__)[:200],
                )
        raise last_error if last_error else RuntimeError("no overpass endpoint configured")

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        subsector = get_registry().get(spec.subsector_key)
        if not subsector.osm_tags or not spec.center:
            return
        query = self._build_query(
            subsector.osm_tags, spec.center.lat, spec.center.lng, int(spec.radius_km * 1000)
        )
        data = await self._query_with_failover(query)
        for el in data.get("elements", []):
            tags = el.get("tags") or {}
            name = tags.get("name")
            if not name:
                continue
            lat = el.get("lat") or (el.get("center") or {}).get("lat")
            lon = el.get("lon") or (el.get("center") or {}).get("lon")
            yield RawRecord(
                source=self.name,
                source_kind=self.kind,
                source_id=f"{el.get('type')}/{el.get('id')}",
                url=f"https://www.openstreetmap.org/{el.get('type')}/{el.get('id')}",
                name=name,
                phone=tags.get("phone") or tags.get("contact:phone"),
                website=tags.get("website") or tags.get("contact:website"),
                address=Address(
                    line1=" ".join(filter(None, [tags.get("addr:housenumber"), tags.get("addr:street")]))
                    or None,
                    city=tags.get("addr:city"),
                    region=tags.get("addr:state") or spec.region,
                    postal_code=tags.get("addr:postcode"),
                    country=tags.get("addr:country") or "US",
                ),
                location=GeoPoint(lat=lat, lng=lon) if lat and lon else None,
                categories=[f"{k}={tags[k]}" for k in ("shop", "craft", "office", "amenity") if k in tags],
                attributes={
                    "brand": tags.get("brand"),
                    "brand_wikidata": tags.get("brand:wikidata"),
                    "opening_hours": tags.get("opening_hours"),
                },
            )
