"""Entity resolution: cluster RawRecords that describe the same business and merge them.

Blocking keys: E.164 phone, website domain, Google place id. Fuzzy linking: similar normalised
names within the same postal code / city or within 300 m. Merge uses per-source trust so a
vendor's NAICS/employee count wins while Google's rating/review count wins.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from rapidfuzz import fuzz

from .geo import haversine_km
from .models import Business, GeoPoint, Provenance, RawRecord, SourceKind
from .normalize import extract_domain, normalize_name, normalize_record

KIND_TRUST = {
    SourceKind.VENDOR: 70,
    SourceKind.REGISTRY: 62,
    SourceKind.MAPS: 60,
    SourceKind.LICENSE: 58,
    SourceKind.IMPORT: 65,
    SourceKind.DIRECTORY: 45,
    SourceKind.ENRICHMENT: 40,
}

NAME_SIMILARITY_THRESHOLD = 88
NEAR_KM = 0.3

# Fields where the "maps" view of the business is authoritative regardless of trust.
MAPS_PREFERRED = {"name", "rating", "review_count", "reviews", "business_status", "opening_hours", "location"}
# Fields where official / list records (registries, licence boards, imports) are authoritative.
RECORD_KINDS = {SourceKind.VENDOR, SourceKind.REGISTRY, SourceKind.LICENSE, SourceKind.IMPORT}
VENDOR_PREFERRED = {
    "naics",
    "employee_count",
    "employee_range",
    "owner_name",
    "owner_title",
    "owner_email",
    "owner_phone",
    "linkedin_url",
}


class UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _trust(record: RawRecord, trust_by_source: dict[str, int]) -> int:
    return trust_by_source.get(record.source, KIND_TRUST.get(record.source_kind, 40))


def cluster_records(records: list[RawRecord]) -> list[list[RawRecord]]:
    uf = UnionFind(len(records))
    by_phone: dict[str, list[int]] = defaultdict(list)
    by_domain: dict[str, list[int]] = defaultdict(list)
    by_place: dict[str, list[int]] = defaultdict(list)
    by_area: dict[str, list[int]] = defaultdict(list)
    names = [normalize_name(r.name) for r in records]

    for i, r in enumerate(records):
        if r.phone:
            by_phone[r.phone].append(i)
        domain = extract_domain(r.website)
        if domain:
            by_domain[domain].append(i)
        if r.source == "google_places" and r.source_id:
            by_place[r.source_id].append(i)
        pid = r.attributes.get("google_place_id")
        if pid:
            by_place[pid].append(i)
        if r.address:
            if r.address.postal_code:
                by_area[f"zip:{r.address.postal_code}"].append(i)
            elif r.address.city and r.address.region:
                by_area[f"city:{r.address.city.lower()}|{r.address.region.upper()}"].append(i)
        if r.location:
            by_area[f"geo:{round(r.location.lat, 2)}|{round(r.location.lng, 2)}"].append(i)

    for group in list(by_phone.values()) + list(by_place.values()):
        for j in group[1:]:
            uf.union(group[0], j)

    # Shared domain alone links records only when names agree and the locations do not
    # contradict each other (chains share one domain across many branches).
    for group in by_domain.values():
        for ai, a in enumerate(group):
            for b in group[ai + 1 :]:
                if fuzz.token_sort_ratio(names[a], names[b]) < 85:
                    continue
                ra, rb = records[a], records[b]
                za = ra.address.postal_code if ra.address else None
                zb = rb.address.postal_code if rb.address else None
                if za and zb and za != zb:
                    continue
                if ra.location and rb.location and haversine_km(ra.location, rb.location) > 5:
                    continue
                uf.union(a, b)

    for group in by_area.values():
        if len(group) > 400:  # pathological block; skip quadratic compare
            continue
        for ai, a in enumerate(group):
            for b in group[ai + 1 :]:
                if uf.find(a) == uf.find(b) or not names[a] or not names[b]:
                    continue
                if fuzz.token_set_ratio(names[a], names[b]) < NAME_SIMILARITY_THRESHOLD:
                    continue
                ra, rb = records[a], records[b]
                if ra.location and rb.location and haversine_km(ra.location, rb.location) > 5:
                    continue
                uf.union(a, b)

    clusters: dict[int, list[RawRecord]] = defaultdict(list)
    for i, r in enumerate(records):
        clusters[uf.find(i)].append(r)
    return list(clusters.values())


def _pick(values: Iterable[tuple[int, object]]) -> object | None:
    ranked = sorted(((t, v) for t, v in values if v not in (None, "", [], {})), key=lambda x: -x[0])
    return ranked[0][1] if ranked else None


def merge_cluster(cluster: list[RawRecord], trust_by_source: dict[str, int] | None = None) -> Business:
    trust_by_source = trust_by_source or {}
    ranked = sorted(cluster, key=lambda r: -_trust(r, trust_by_source))

    def field(name: str) -> object | None:
        preferred_kinds: set[SourceKind] = set()
        if name in MAPS_PREFERRED:
            preferred_kinds = {SourceKind.MAPS}
        elif name in VENDOR_PREFERRED:
            preferred_kinds = RECORD_KINDS
        candidates = []
        for r in ranked:
            bonus = 100 if r.source_kind in preferred_kinds else 0
            if name == "review_count" and r.source == "google_places":
                bonus += 50
            candidates.append((_trust(r, trust_by_source) + bonus, getattr(r, name)))
        return _pick(candidates)

    name = str(field("name") or cluster[0].name)
    categories: list[str] = []
    for r in ranked:
        for c in r.categories:
            if c and c not in categories:
                categories.append(c)
    reviews = []
    seen_reviews: set[tuple[str | None, str | None]] = set()
    for r in ranked:
        for rv in r.reviews:
            key = (rv.author, (rv.text or "")[:60])
            if key not in seen_reviews:
                seen_reviews.add(key)
                reviews.append(rv)
    attributes: dict[str, object] = {}
    for r in reversed(ranked):
        attributes.update({k: v for k, v in r.attributes.items() if v not in (None, "", [], {})})
    place = next((r for r in ranked if r.source == "google_places"), None)
    website = field("website")
    business = Business(
        name=name,
        normalized_name=normalize_name(name),
        phone_e164=field("phone"),  # type: ignore[arg-type]
        website=website,  # type: ignore[arg-type]
        domain=extract_domain(website) if isinstance(website, str) else None,
        address=field("address"),  # type: ignore[arg-type]
        location=field("location"),  # type: ignore[arg-type]
        google_place_id=place.source_id if place else attributes.get("google_place_id"),  # type: ignore[arg-type]
        categories=categories,
        naics=field("naics"),  # type: ignore[arg-type]
        employee_count=field("employee_count"),  # type: ignore[arg-type]
        employee_range=field("employee_range"),  # type: ignore[arg-type]
        rating=field("rating"),  # type: ignore[arg-type]
        review_count=field("review_count"),  # type: ignore[arg-type]
        reviews=reviews,
        business_status=field("business_status"),  # type: ignore[arg-type]
        license_number=field("license_number"),  # type: ignore[arg-type]
        license_status=field("license_status"),  # type: ignore[arg-type]
        provenance=[
            Provenance(
                source=r.source,
                source_kind=r.source_kind,
                source_id=r.source_id,
                url=r.url,
                fetched_at=r.fetched_at,
            )
            for r in cluster
        ],
        attributes=attributes,
    )
    owner_name = field("owner_name")
    if owner_name:
        business.owner.name = str(owner_name)
        business.owner.title = field("owner_title")  # type: ignore[assignment]
        business.owner.email = field("owner_email")  # type: ignore[assignment]
        business.owner.phone = field("owner_phone")  # type: ignore[assignment]
        business.owner.linkedin_url = field("linkedin_url")  # type: ignore[assignment]
        src = next((r.source for r in ranked if r.owner_name), None)
        business.owner.source = src
        business.owner.confidence = 0.8 if business.owner.email else 0.5
    if business.location is None:
        for r in ranked:
            if r.location:
                business.location = GeoPoint(lat=r.location.lat, lng=r.location.lng)
                break
    return business


def attach_record(business: Business, record: RawRecord) -> Business:
    """Merge a late observation (e.g. the Google Maps listing found for a registry record) into an
    existing Business: Maps-preferred fields are taken from the record, everything else only fills gaps."""
    rec = normalize_record(record)
    is_maps = rec.source_kind == SourceKind.MAPS
    if is_maps and rec.name:
        business.name = rec.name
        business.normalized_name = normalize_name(rec.name)
    if rec.website and (is_maps or not business.website):
        business.website = rec.website
        business.domain = extract_domain(rec.website)
    if rec.phone and (is_maps or not business.phone_e164):
        business.phone_e164 = rec.phone
    if rec.address and (is_maps or business.address is None):
        business.address = rec.address
    if rec.location and (is_maps or business.location is None):
        business.location = GeoPoint(lat=rec.location.lat, lng=rec.location.lng)
    if rec.source == "google_places" and rec.source_id:
        business.google_place_id = rec.source_id
    for name in ("rating", "review_count", "business_status"):
        value = getattr(rec, name)
        if value is not None and (is_maps or getattr(business, name) is None):
            setattr(business, name, value)
    for c in rec.categories:
        if c and c not in business.categories:
            business.categories.append(c)
    for rv in rec.reviews:
        business.reviews.append(rv)
    business.attributes.update({k: v for k, v in rec.attributes.items() if v not in (None, "", [], {})})
    business.provenance.append(
        Provenance(
            source=rec.source,
            source_kind=rec.source_kind,
            source_id=rec.source_id,
            url=rec.url,
            fetched_at=rec.fetched_at,
        )
    )
    return business


def resolve(records: Iterable[RawRecord], trust_by_source: dict[str, int] | None = None) -> list[Business]:
    normalized = [normalize_record(r) for r in records if r.name and r.name.strip()]
    return [merge_cluster(c, trust_by_source) for c in cluster_records(normalized)]
