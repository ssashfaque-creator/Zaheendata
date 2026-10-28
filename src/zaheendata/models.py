from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any


EXPORT_COLUMNS = [
    "name",
    "phone",
    "website",
    "website_crawled",
    "address",
    "city",
    "state",
    "postal_code",
    "owner_name",
    "owner_email",
    "owner_phone",
    "employee_count",
    "naics",
    "score_total",
    "rank",
    "status",
    "sources",
    "lat",
    "lng",
    "domain",
    "google_place_id",
    "categories",
    "owner_title",
    "owner_linkedin",
    "owner_source",
    "owner_confidence",
    "email_is_role_account",
    "suppressed",
    "suppression_reasons",
    "rejection_reason",
    "license_number",
    "license_status",
    "business_id",
    "online_booking",
    "booking_provider",
    "quote_form",
    "says_call_for_quote",
    "emergency_service",
    "maintenance_plan",
    "recurring_service",
    "free_estimate",
    "online_store",
    "store_platform",
    "sells_gift_cards",
    "hosts_events",
    "instagram",
    "tech_stack",
    "tool_categories",
    "pain_mentions",
    "pain_examples",
    "google_rating",
    "google_reviews",
    "recent_reviews_90d",
    "last_review_at",
    "busy_and_growing",
    "admin_pain",
    "multi_tool",
    "owner_reachable",
    "subsector_signal",
    "score_evidence",
]


@dataclass
class Lead:
    name: str = ""
    phone: str = ""
    website: str = ""
    website_crawled: bool = False
    address: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    owner_name: str = ""
    owner_email: str = ""
    owner_phone: str = ""
    employee_count: str = ""
    naics: str = ""
    score_total: int | str = ""
    rank: int | str = ""
    status: str = ""
    sources: str = ""
    lat: float | str = ""
    lng: float | str = ""
    domain: str = ""
    google_place_id: str = ""
    categories: str = ""
    owner_title: str = ""
    owner_linkedin: str = ""
    owner_source: str = ""
    owner_confidence: float | str = ""
    email_is_role_account: bool = False
    suppressed: bool = False
    suppression_reasons: str = ""
    rejection_reason: str = ""
    license_number: str = ""
    license_status: str = ""
    business_id: str = ""
    online_booking: bool | str = ""
    booking_provider: str = ""
    quote_form: bool | str = ""
    says_call_for_quote: bool | str = ""
    emergency_service: bool | str = ""
    maintenance_plan: bool | str = ""
    recurring_service: bool | str = ""
    free_estimate: bool | str = ""
    online_store: bool | str = ""
    store_platform: str = ""
    sells_gift_cards: bool | str = ""
    hosts_events: bool | str = ""
    instagram: str = ""
    tech_stack: str = ""
    tool_categories: str = ""
    pain_mentions: str = ""
    pain_examples: str = ""
    google_rating: float | str = ""
    google_reviews: int | str = ""
    recent_reviews_90d: int | str = ""
    last_review_at: str = ""
    busy_and_growing: int | str = ""
    admin_pain: int | str = ""
    multi_tool: int | str = ""
    owner_reachable: int | str = ""
    subsector_signal: int | str = ""
    score_evidence: str = ""
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def merge_source(self, label: str) -> None:
        existing = [part.strip() for part in self.sources.split(";") if part.strip()]
        if label and label not in existing:
            existing.append(label)
        self.sources = "; ".join(existing)

    def to_export_row(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("extra", None)
        return {column: data.get(column, "") for column in EXPORT_COLUMNS}


def empty_lead(**kwargs: Any) -> Lead:
    known = {item.name for item in fields(Lead)}
    extra = {key: value for key, value in kwargs.items() if key not in known}
    filtered = {key: value for key, value in kwargs.items() if key in known}
    lead = Lead(**filtered)
    lead.extra.update(extra)
    return lead
