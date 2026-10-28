from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from zaheendata.cache import JsonCache
from zaheendata.models import Lead, empty_lead
from zaheendata.normalize import canonicalize_website, domain_from_url, normalize_phone
from zaheendata.subsectors import CITY_CENTERS, Subsector

PLACES_TEXT_SEARCH = "https://places.googleapis.com/v1/places:searchText"

# Enterprise SKU: website + rating + review count in one search call.
# Avoid a second Place Details round-trip per business.
FIELD_MASK = ",".join(
    [
        "nextPageToken",
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.location",
        "places.types",
        "places.nationalPhoneNumber",
        "places.internationalPhoneNumber",
        "places.websiteUri",
        "places.rating",
        "places.userRatingCount",
        "places.businessStatus",
        "places.googleMapsUri",
        "places.reviews",
        "places.primaryType",
        "places.addressComponents",
    ]
)


class PlacesConfigError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not key:
        raise PlacesConfigError(
            "GOOGLE_MAPS_API_KEY is not set. Places is the discovery source "
            "that already has website, phone, rating and review count."
        )
    return key


def _city_bias(city: str, state: str) -> dict[str, Any]:
    lat, lng, radius = CITY_CENTERS.get((city.lower(), state.lower()), (None, None, None))
    if lat is None:
        return {}
    return {
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lng},
                "radius": float(radius),
            }
        }
    }


def _component(components: list[dict[str, Any]], kind: str) -> str:
    for item in components or []:
        if kind in item.get("types", []):
            return item.get("shortText") or item.get("longText") or ""
    return ""


def _street(components: list[dict[str, Any]]) -> str:
    number = _component(components, "street_number")
    route = _component(components, "route")
    return " ".join(part for part in (number, route) if part).strip()


def _recent_reviews(reviews: list[dict[str, Any]], days: int = 90) -> tuple[int, str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    count = 0
    latest = ""
    for review in reviews or []:
        raw = (review.get("publishTime") or "")[:25]
        if not raw:
            continue
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        stamp = published.date().isoformat()
        if not latest or stamp > latest:
            latest = stamp
        if published >= cutoff:
            count += 1
    return count, latest


def place_to_lead(place: dict[str, Any], subsector: Subsector) -> Lead:
    display = place.get("displayName") or {}
    name = display.get("text") if isinstance(display, dict) else str(display or "")
    components = place.get("addressComponents") or []
    website = canonicalize_website(place.get("websiteUri"))
    location = place.get("location") or {}
    reviews = place.get("reviews") or []
    recent, last_review = _recent_reviews(reviews)
    types = place.get("types") or []
    phone = normalize_phone(place.get("nationalPhoneNumber") or place.get("internationalPhoneNumber"))
    lead = empty_lead(
        name=name or "",
        phone=phone,
        website=website,
        address=_street(components) or (place.get("formattedAddress") or "").split(",")[0],
        city=_component(components, "locality") or _component(components, "postal_town"),
        state=_component(components, "administrative_area_level_1"),
        postal_code=_component(components, "postal_code"),
        naics=subsector.naics,
        google_place_id=(place.get("id") or "").replace("places/", ""),
        domain=domain_from_url(website),
        categories=",".join(types),
        google_rating=place.get("rating") or "",
        google_reviews=place.get("userRatingCount") if place.get("userRatingCount") is not None else "",
        recent_reviews_90d=recent,
        last_review_at=last_review,
        lat=location.get("latitude", ""),
        lng=location.get("longitude", ""),
        status="discovered",
    )
    lead.merge_source("google_places")
    if place.get("businessStatus"):
        lead.extra["business_status"] = place.get("businessStatus")
    return lead


def iter_text_search(
    client: httpx.Client,
    *,
    text_query: str,
    included_type: str | None,
    city: str,
    state: str,
    api_key: str,
    cache: JsonCache | None,
    max_pages: int = 8,
) -> Iterable[dict[str, Any]]:
    body: dict[str, Any] = {
        "textQuery": f"{text_query} in {city} {state}",
        "pageSize": 20,
        "languageCode": "en",
        "regionCode": "US",
        **_city_bias(city, state),
    }
    if included_type:
        body["includedType"] = included_type
        body["strictTypeFiltering"] = True

    page_token = ""
    for page in range(max_pages):
        payload = dict(body)
        if page_token:
            payload["pageToken"] = page_token
        cache_key = f"{payload.get('textQuery')}|{included_type}|{page}|{payload.get('pageToken','')}"
        cached = cache.get("places", cache_key) if cache else None
        if cached is None:
            response = client.post(
                PLACES_TEXT_SEARCH,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": FIELD_MASK,
                },
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            cached = response.json()
            if cache:
                cache.set("places", cache_key, cached)
        for place in cached.get("places") or []:
            yield place
        page_token = cached.get("nextPageToken") or ""
        if not page_token:
            break


def discover_places(
    subsector: Subsector,
    *,
    city: str,
    state: str,
    cache: JsonCache | None = None,
    client: httpx.Client | None = None,
    max_pages: int = 8,
) -> list[Lead]:
    api_key = _api_key()
    owned_client = client is None
    client = client or httpx.Client()
    seen: dict[str, Lead] = {}
    try:
        pairs: list[tuple[str, str | None]] = []
        types = list(subsector.google_types)
        for index, query in enumerate(subsector.search_queries):
            included = types[index] if index < len(types) else None
            pairs.append((query, included))
        for query, included in pairs:
            for place in iter_text_search(
                client,
                text_query=query,
                included_type=included,
                city=city,
                state=state,
                api_key=api_key,
                cache=cache,
                max_pages=max_pages,
            ):
                lead = place_to_lead(place, subsector)
                key = lead.google_place_id or lead.name.lower()
                if key in seen:
                    continue
                if (lead.extra.get("business_status") or "OPERATIONAL") not in {"OPERATIONAL", ""}:
                    continue
                seen[key] = lead
    finally:
        if owned_client:
            client.close()
    return list(seen.values())
