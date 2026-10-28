"""Google Places API (New) connector: the compliant way to get Google Maps data.

Text Search gives up to 60 results per query/cell (3 pages of 20). Reviews are fetched per place
through the (still supported) legacy Place Details endpoint because it accepts
``reviews_sort=newest``, which is what the "recent reviews" signal needs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from rapidfuzz import fuzz

from ..logging import get_logger
from ..models import Address, GeoPoint, RawRecord, Review, SourceKind, SourceSearchSpec
from ..normalize import normalize_name
from ..settings import Settings
from .base import Source, SourceContext

log = get_logger(__name__)

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
MATCH_THRESHOLD = 82  # token_set_ratio needed to accept a Maps listing for a registry record

SEARCH_FIELD_MASK = (
    ",".join(
        f"places.{f}"
        for f in (
            "id",
            "displayName",
            "formattedAddress",
            "addressComponents",
            "location",
            "nationalPhoneNumber",
            "internationalPhoneNumber",
            "websiteUri",
            "rating",
            "userRatingCount",
            "businessStatus",
            "primaryType",
            "types",
            "googleMapsUri",
            "regularOpeningHours.weekdayDescriptions",
        )
    )
    + ",nextPageToken"
)


def _component(components: list[dict[str, Any]], kind: str, short: bool = True) -> str | None:
    for c in components or []:
        if kind in c.get("types", []):
            return c.get("shortText" if short else "longText")
    return None


def parse_place(place: dict[str, Any]) -> RawRecord:
    comps = place.get("addressComponents") or []
    loc = place.get("location") or {}
    line1 = " ".join(filter(None, [_component(comps, "street_number"), _component(comps, "route")])) or None
    return RawRecord(
        source="google_places",
        source_kind=SourceKind.MAPS,
        source_id=place.get("id"),
        url=place.get("googleMapsUri"),
        name=(place.get("displayName") or {}).get("text") or "",
        phone=place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"),
        website=place.get("websiteUri"),
        address=Address(
            line1=line1,
            city=_component(comps, "locality") or _component(comps, "sublocality"),
            region=_component(comps, "administrative_area_level_1"),
            postal_code=_component(comps, "postal_code"),
            country=_component(comps, "country") or "US",
            formatted=place.get("formattedAddress"),
        ),
        location=GeoPoint(lat=loc["latitude"], lng=loc["longitude"]) if loc else None,
        categories=[t for t in place.get("types", []) if t not in ("point_of_interest", "establishment")],
        rating=place.get("rating"),
        review_count=place.get("userRatingCount"),
        business_status=place.get("businessStatus"),
        opening_hours=place.get("regularOpeningHours"),
        attributes={"primary_type": place.get("primaryType")},
    )


class GooglePlacesSource(Source):
    name = "google_places"
    kind = SourceKind.MAPS
    tags: ClassVar[set[str]] = {"google_places", "google_maps"}
    description = "Google Places API (New) text search + place details (reviews, newest first)."
    trust = 80
    always_on = True

    def __init__(self, ctx: SourceContext) -> None:
        super().__init__(ctx)
        rps = max(0.05, self.settings.google_places_requests_per_second)
        self.http.set_host_rate("places.googleapis.com", rps, concurrency=1)
        self.http.set_host_rate("maps.googleapis.com", min(rps * 2, 1.0), concurrency=1)

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return settings.google_maps_api_key is not None

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "set ZAHEEN_GOOGLE_MAPS_API_KEY"

    def _headers(self, field_mask: str) -> dict[str, str]:
        return {
            "X-Goog-Api-Key": self.settings.secret("google_maps_api_key") or "",
            "X-Goog-FieldMask": field_mask,
            "Content-Type": "application/json",
        }

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        seen: set[str] = set()
        for query in spec.queries:
            text = query if not spec.location_text else f"{query} in {spec.location_text}"
            page_token: str | None = None
            yielded = 0
            for _page in range(3):
                body: dict[str, Any] = {"textQuery": text, "pageSize": 20, "languageCode": "en"}
                if spec.center:
                    body["locationBias"] = {
                        "circle": {
                            "center": {"latitude": spec.center.lat, "longitude": spec.center.lng},
                            "radius": min(spec.radius_km * 1000, 50_000),
                        }
                    }
                if page_token:
                    body["pageToken"] = page_token
                data = await self.http.post_json(
                    SEARCH_URL,
                    json_body=body,
                    headers=self._headers(SEARCH_FIELD_MASK),
                    meter="google_places.text_search",
                )
                for place in data.get("places", []):
                    pid = place.get("id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    record = parse_place(place)
                    if record.name:
                        yielded += 1
                        yield record
                page_token = data.get("nextPageToken")
                if not page_token or yielded >= spec.limit:
                    break

    async def match_business(
        self, name: str, city: str | None, region: str | None, postal_code: str | None = None
    ) -> RawRecord | None:
        """Find the Google Maps listing for a registry / licence record (name + city + state).

        One Text Search call per lookup. Accepts a candidate only when the name is a close fuzzy match
        and the state agrees, so a registry's legal name does not get attached to a stranger's listing."""
        where = ", ".join(filter(None, [city, region])) or (postal_code or "")
        text = f"{name} {where}".strip()
        body: dict[str, Any] = {"textQuery": text, "pageSize": 5, "languageCode": "en"}
        data = await self.http.post_json(
            SEARCH_URL,
            json_body=body,
            headers=self._headers(SEARCH_FIELD_MASK),
            meter="google_places.text_search",
        )
        target = normalize_name(name)
        best: tuple[float, RawRecord] | None = None
        for place in data.get("places", []):
            rec = parse_place(place)
            if not rec.name:
                continue
            if region and rec.address and rec.address.region and rec.address.region.upper() != region.upper():
                continue
            score = float(fuzz.token_set_ratio(target, normalize_name(rec.name)))
            if postal_code and rec.address and rec.address.postal_code == postal_code[:5]:
                score += 5
            if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
                best = (score, rec)
        return best[1] if best else None

    async def fetch_reviews(self, place_id: str, max_reviews: int = 5) -> tuple[list[Review], dict[str, Any]]:
        data = await self.http.get_json(
            DETAILS_URL,
            params={
                "place_id": place_id,
                "fields": "reviews,user_ratings_total,rating,business_status",
                "reviews_sort": "newest",
                "key": self.settings.secret("google_maps_api_key"),
            },
            meter="google_places.details",
        )
        result = data.get("result") or {}
        reviews = [
            Review(
                source="google",
                author=r.get("author_name"),
                rating=r.get("rating"),
                text=r.get("text"),
                published_at=datetime.fromtimestamp(r["time"], tz=UTC) if r.get("time") else None,
            )
            for r in (result.get("reviews") or [])[:max_reviews]
        ]
        return reviews, {
            "rating": result.get("rating"),
            "review_count": result.get("user_ratings_total"),
            "business_status": result.get("business_status"),
        }
