"""Domain models shared by every layer (sources, pipeline, storage, API)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return uuid.uuid4().hex


class SourceKind(StrEnum):
    REGISTRY = "registry"  # government registries: tax permits, business licences, SoS, SAM.gov, SBA PPP
    LICENSE = "license"  # state contractor / cosmetology / pest-control licence boards
    MAPS = "maps"  # Google Places / Maps, Yelp, OSM
    DIRECTORY = "directory"  # trade-association directories, store locators
    IMPORT = "import"  # CSV / spreadsheet uploads (any origin)
    VENDOR = "vendor"  # reserved for imported commercial lists
    ENRICHMENT = "enrichment"  # website crawl, registry officer lookups


class GeoPoint(BaseModel):
    lat: float
    lng: float


class Address(BaseModel):
    line1: str | None = None
    city: str | None = None
    region: str | None = None  # state / province code, e.g. "TX"
    postal_code: str | None = None
    country: str | None = "US"
    formatted: str | None = None


class Review(BaseModel):
    source: str = "google"
    author: str | None = None
    rating: float | None = None
    text: str | None = None
    published_at: datetime | None = None


class RawRecord(BaseModel):
    """A single observation of a business from one source, before entity resolution."""

    source: str
    source_kind: SourceKind
    source_id: str | None = None
    url: str | None = None
    name: str
    phone: str | None = None
    website: str | None = None
    address: Address | None = None
    location: GeoPoint | None = None
    categories: list[str] = Field(default_factory=list)
    naics: str | None = None
    employee_count: int | None = None
    employee_range: str | None = None
    rating: float | None = None
    review_count: int | None = None
    reviews: list[Review] = Field(default_factory=list)
    business_status: str | None = None
    opening_hours: dict[str, Any] | None = None
    owner_name: str | None = None
    owner_title: str | None = None
    owner_email: str | None = None
    owner_phone: str | None = None
    linkedin_url: str | None = None
    license_number: str | None = None
    license_status: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime = Field(default_factory=utcnow)


class Provenance(BaseModel):
    source: str
    source_kind: SourceKind
    source_id: str | None = None
    url: str | None = None
    fetched_at: datetime


class WebsiteFeatures(BaseModel):
    crawled: bool = False
    final_url: str | None = None
    pages_crawled: int = 0
    platform: str | None = None  # wordpress / wix / squarespace / shopify ...
    has_online_booking: bool = False
    booking_provider: str | None = None
    has_quote_form: bool = False
    has_contact_form: bool = False
    says_call_for_quote: bool = False
    free_estimate: bool = False
    emergency_service: bool = False
    maintenance_plan: bool = False
    recurring_service: bool = False
    financing: bool = False
    insurance_storm_work: bool = False
    inspection_request: bool = False
    residential_focus: bool = False
    commercial_focus: bool = False
    project_gallery: bool = False
    sells_gift_cards: bool = False
    walk_ins_welcome: bool = False
    retail_products: bool = False
    online_store: bool = False
    store_platform: str | None = None  # shopify / etsy / square-online / woocommerce
    hosts_events: bool = False
    wedding_event_work: bool = False
    team_uniform_sales: bool = False
    grooming_services: bool = False
    multiple_crews_or_staff: bool = False
    commercial_clients: bool = False
    franchise_language: bool = False
    locally_owned_language: bool = False
    location_count_hint: int | None = None
    chair_count_hint: int | None = None
    owner_name_candidates: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    street_address: str | None = None
    address_city: str | None = None
    address_region: str | None = None
    address_postal: str | None = None
    social_links: dict[str, str] = Field(default_factory=dict)
    instagram_shop_signals: bool = False
    error: str | None = None


class Technology(BaseModel):
    name: str
    category: str
    confidence: float = 1.0
    source: str = "fingerprint"


class TechStack(BaseModel):
    technologies: list[Technology] = Field(default_factory=list)

    @property
    def categories(self) -> set[str]:
        return {t.category for t in self.technologies}

    @property
    def names(self) -> set[str]:
        return {t.name for t in self.technologies}


class ReviewInsights(BaseModel):
    analysed_reviews: int = 0
    recent_reviews: int = 0
    last_review_at: datetime | None = None
    pain_mentions: int = 0
    pain_examples: list[str] = Field(default_factory=list)
    pain_categories: dict[str, int] = Field(default_factory=dict)


class OwnerContact(BaseModel):
    name: str | None = None
    title: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    source: str | None = None
    confidence: float = 0.0


class BusinessStatus(StrEnum):
    CANDIDATE = "candidate"
    REJECTED = "rejected"
    SCORED = "scored"
    SELECTED = "selected"


class SignalScore(BaseModel):
    name: str
    points: int = Field(ge=0, le=2)
    evidence: list[str] = Field(default_factory=list)


class FitScore(BaseModel):
    signals: list[SignalScore] = Field(default_factory=list)
    total: int = 0
    rank: int | None = None

    def by_name(self) -> dict[str, int]:
        return {s.name: s.points for s in self.signals}


class Business(BaseModel):
    """A resolved business entity: the merge of one or more RawRecords plus enrichment."""

    id: str = Field(default_factory=new_id)
    campaign_id: str | None = None
    name: str
    normalized_name: str = ""
    phone_e164: str | None = None
    website: str | None = None
    domain: str | None = None
    address: Address | None = None
    location: GeoPoint | None = None
    google_place_id: str | None = None
    categories: list[str] = Field(default_factory=list)
    naics: str | None = None
    employee_count: int | None = None
    employee_range: str | None = None
    rating: float | None = None
    review_count: int | None = None
    reviews: list[Review] = Field(default_factory=list)
    business_status: str | None = None
    license_number: str | None = None
    license_status: str | None = None
    provenance: list[Provenance] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    website_features: WebsiteFeatures = Field(default_factory=WebsiteFeatures)
    tech_stack: TechStack = Field(default_factory=TechStack)
    review_insights: ReviewInsights = Field(default_factory=ReviewInsights)
    owner: OwnerContact = Field(default_factory=OwnerContact)
    status: BusinessStatus = BusinessStatus.CANDIDATE
    rejection_reason: str | None = None
    score: FitScore | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for p in self.provenance:
            if p.source not in seen:
                seen.append(p.source)
        return seen


class GeoScope(StrEnum):
    METRO = "metro"
    STATE = "state"
    NATIONAL = "national"


class GeographySpec(BaseModel):
    """Where to search. Provide one of: ``locations`` (city, ST), ``state`` (ST) or ``country``.
    ``radius_km`` applies to each location; grids are generated for state / national scope."""

    locations: list[str] = Field(default_factory=list)
    state: str | None = None
    country: str = "US"
    radius_km: float = 25.0
    grid_cell_km: float = 40.0

    @property
    def scope(self) -> GeoScope:
        if self.locations:
            return GeoScope.METRO
        if self.state:
            return GeoScope.STATE
        return GeoScope.NATIONAL

    def describe(self) -> str:
        if self.locations:
            return ", ".join(self.locations)
        if self.state:
            return f"state:{self.state}"
        return f"country:{self.country}"


class CampaignSpec(BaseModel):
    name: str
    subsector_key: str
    geography: GeographySpec
    long_list_target: int | None = None
    short_list_target: int | None = None
    sources: list[str] | None = None  # None -> all enabled sources suited to the sub-sector
    enrich: bool = True
    contact_enrichment: bool = True
    notes: str | None = None


class CampaignState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Campaign(BaseModel):
    id: str = Field(default_factory=new_id)
    spec: CampaignSpec
    state: CampaignState = CampaignState.CREATED
    stage: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class SourceSearchSpec(BaseModel):
    """What a source connector is asked to find for one geographic cell."""

    subsector_key: str
    naics: str
    queries: list[str]
    center: GeoPoint | None = None
    radius_km: float = 25.0
    location_text: str | None = None  # "Austin, TX"
    region: str | None = None  # state code
    county: str | None = None  # "Travis" (metro searches only)
    country: str = "US"
    employee_min: int | None = None
    employee_max: int | None = None
    limit: int = 60
