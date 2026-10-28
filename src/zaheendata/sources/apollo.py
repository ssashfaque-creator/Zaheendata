"""Apollo.io connector: organization search for the long list and people search / match for
owner contact enrichment (name, title, email, direct phone, LinkedIn)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from ..logging import get_logger
from ..models import Address, OwnerContact, RawRecord, SourceKind, SourceSearchSpec
from ..registry import get_registry
from ..settings import Settings
from .base import Source

log = get_logger(__name__)

BASE = "https://api.apollo.io/api/v1"
OWNER_TITLES = [
    "owner",
    "co-owner",
    "founder",
    "co-founder",
    "president",
    "ceo",
    "principal",
    "managing partner",
]


class ApolloSource(Source):
    name = "apollo"
    kind = SourceKind.VENDOR
    tags: ClassVar[set[str]] = {"apollo", "vendor"}
    description = "Apollo.io organisations (employee range, location, keywords) + owner contacts."
    trust = 70
    always_on = True

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return settings.apollo_api_key is not None

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "set ZAHEEN_APOLLO_API_KEY"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.settings.secret("apollo_api_key") or "",
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
        }

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        subsector = get_registry().get(spec.subsector_key)
        location = spec.location_text or (spec.region and f"{spec.region}, US") or "United States"
        emp_min = spec.employee_min or self.settings.employee_min
        emp_max = spec.employee_max or self.settings.employee_max
        seen: set[str] = set()
        for page in range(1, 6):
            body: dict[str, Any] = {
                "page": page,
                "per_page": 100,
                "organization_locations": [location],
                "organization_num_employees_ranges": [f"{emp_min},{emp_max}"],
                "q_organization_keyword_tags": subsector.apollo_keywords or spec.queries,
            }
            data = await self.http.post_json(
                f"{BASE}/mixed_companies/search",
                json_body=body,
                headers=self._headers(),
                meter="apollo.org_search",
            )
            orgs = data.get("organizations") or data.get("accounts") or []
            for org in orgs:
                oid = org.get("id")
                if not oid or oid in seen:
                    continue
                seen.add(oid)
                yield RawRecord(
                    source=self.name,
                    source_kind=self.kind,
                    source_id=oid,
                    url=org.get("linkedin_url"),
                    name=org.get("name") or "",
                    phone=(org.get("primary_phone") or {}).get("number") or org.get("phone"),
                    website=org.get("website_url"),
                    address=Address(
                        line1=org.get("street_address"),
                        city=org.get("city"),
                        region=org.get("state"),
                        postal_code=org.get("postal_code"),
                        country=org.get("country") or "US",
                    ),
                    categories=[org.get("industry")] if org.get("industry") else [],
                    naics=spec.naics,
                    employee_count=org.get("estimated_num_employees"),
                    linkedin_url=org.get("linkedin_url"),
                    attributes={
                        "apollo_organization_id": oid,
                        "founded_year": org.get("founded_year"),
                        "keywords": org.get("keywords"),
                        "primary_domain": org.get("primary_domain"),
                    },
                )
            pagination = data.get("pagination") or {}
            if page >= int(pagination.get("total_pages") or 1) or len(seen) >= spec.limit:
                break

    async def find_owner(
        self, domain: str | None, org_name: str | None, reveal: bool = True
    ) -> OwnerContact | None:
        if not domain and not org_name:
            return None
        body: dict[str, Any] = {"page": 1, "per_page": 5, "person_titles": OWNER_TITLES}
        if domain:
            body["q_organization_domains"] = domain
        else:
            body["q_organization_name"] = org_name
        data = await self.http.post_json(
            f"{BASE}/mixed_people/search",
            json_body=body,
            headers=self._headers(),
            meter="apollo.people_search",
        )
        people = data.get("people") or []
        if not people:
            return None
        person = people[0]
        contact = OwnerContact(
            name=" ".join(filter(None, [person.get("first_name"), person.get("last_name")])) or None,
            title=person.get("title"),
            linkedin_url=person.get("linkedin_url"),
            source=self.name,
            confidence=0.7,
        )
        if reveal and person.get("id"):
            try:
                match = await self.http.post_json(
                    f"{BASE}/people/match",
                    json_body={
                        "id": person["id"],
                        "reveal_personal_emails": False,
                        "reveal_phone_number": False,
                    },
                    headers=self._headers(),
                    meter="apollo.people_match",
                )
                p = match.get("person") or {}
                contact.email = p.get("email") or contact.email
                phones = p.get("phone_numbers") or []
                contact.phone = next(
                    (ph.get("sanitized_number") for ph in phones if ph.get("sanitized_number")), None
                )
                contact.confidence = 0.9 if contact.email else contact.confidence
            except Exception as exc:  # noqa: BLE001 - enrichment is best-effort
                log.warning("apollo.match_failed", error=str(exc))
        return contact
