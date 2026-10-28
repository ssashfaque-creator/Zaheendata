"""Yelp Fusion API connector (official, free tier available)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import ClassVar

from rapidfuzz import fuzz

from ..models import Address, GeoPoint, RawRecord, SourceKind, SourceSearchSpec
from ..normalize import normalize_name
from ..registry import get_registry
from ..settings import Settings
from .base import Source

SEARCH_URL = "https://api.yelp.com/v3/businesses/search"


class YelpSource(Source):
    name = "yelp"
    kind = SourceKind.MAPS
    tags: ClassVar[set[str]] = {"yelp"}
    description = "Yelp Fusion business search (ratings, review counts, categories, phone, address)."
    trust = 60

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return bool(settings.secret("yelp_api_key"))

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "set ZAHEEN_YELP_API_KEY"

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        key = self.settings.secret("yelp_api_key")
        if not key:
            return
        subsector = get_registry().get(spec.subsector_key)
        headers = {"Authorization": f"Bearer {key}"}
        seen: set[str] = set()
        terms = [None] if subsector.yelp_categories else spec.queries
        for term in terms:
            offset = 0
            while offset < min(spec.limit * 2, 1000):
                params: dict[str, object] = {"limit": 50, "offset": offset, "sort_by": "review_count"}
                if subsector.yelp_categories:
                    params["categories"] = ",".join(subsector.yelp_categories)
                if term:
                    params["term"] = term
                if spec.center:
                    params["latitude"] = spec.center.lat
                    params["longitude"] = spec.center.lng
                    params["radius"] = int(min(spec.radius_km * 1000, 40_000))
                elif spec.location_text:
                    params["location"] = spec.location_text
                else:
                    break
                data = await self.http.get_json(
                    SEARCH_URL, params=params, headers=headers, meter="yelp.search"
                )
                businesses = data.get("businesses", [])
                for b in businesses:
                    if b["id"] in seen:
                        continue
                    seen.add(b["id"])
                    loc = b.get("location") or {}
                    coords = b.get("coordinates") or {}
                    yield RawRecord(
                        source=self.name,
                        source_kind=self.kind,
                        source_id=b["id"],
                        url=b.get("url"),
                        name=b.get("name", ""),
                        phone=b.get("display_phone") or b.get("phone"),
                        address=Address(
                            line1=loc.get("address1"),
                            city=loc.get("city"),
                            region=loc.get("state"),
                            postal_code=loc.get("zip_code"),
                            country=loc.get("country") or "US",
                            formatted=", ".join(loc.get("display_address") or []),
                        ),
                        location=GeoPoint(lat=coords["latitude"], lng=coords["longitude"])
                        if coords.get("latitude") is not None
                        else None,
                        categories=[c["alias"] for c in b.get("categories", [])],
                        rating=b.get("rating"),
                        review_count=b.get("review_count"),
                        business_status="CLOSED_PERMANENTLY" if b.get("is_closed") else "OPERATIONAL",
                        attributes={"yelp_review_count": b.get("review_count"), "price": b.get("price")},
                    )
                if len(businesses) < 50:
                    break
                offset += 50

    async def match_business(self, name: str, city: str | None, region: str | None) -> RawRecord | None:
        """One Fusion search for a registry name. Free tier; used during enrich, not collect."""
        where = ", ".join(p for p in (city, region) if p)
        key = self.settings.secret("yelp_api_key")
        if not where or not key:
            return None
        headers = {"Authorization": f"Bearer {key}"}
        data = await self.http.get_json(
            SEARCH_URL,
            params={"term": name, "location": where, "limit": 5},
            headers=headers,
            meter="yelp.search",
        )
        target = normalize_name(name)
        best: tuple[float, RawRecord] | None = None
        for b in data.get("businesses") or []:
            loc = b.get("location") or {}
            coords = b.get("coordinates") or {}
            rec = RawRecord(
                source=self.name,
                source_kind=self.kind,
                source_id=b.get("id"),
                url=b.get("url"),
                name=b.get("name") or "",
                phone=b.get("display_phone") or b.get("phone"),
                address=Address(
                    line1=loc.get("address1"),
                    city=loc.get("city"),
                    region=loc.get("state"),
                    postal_code=loc.get("zip_code"),
                    country=loc.get("country") or "US",
                    formatted=", ".join(loc.get("display_address") or []),
                ),
                location=GeoPoint(lat=coords["latitude"], lng=coords["longitude"])
                if coords.get("latitude") is not None
                else None,
                categories=[c["alias"] for c in b.get("categories", [])],
                rating=b.get("rating"),
                review_count=b.get("review_count"),
                business_status="CLOSED_PERMANENTLY" if b.get("is_closed") else "OPERATIONAL",
            )
            if region and rec.address and rec.address.region and rec.address.region.upper() != region.upper():
                continue
            score = float(fuzz.token_set_ratio(target, normalize_name(rec.name)))
            if score >= 82 and (best is None or score > best[0]):
                best = (score, rec)
        return best[1] if best else None
