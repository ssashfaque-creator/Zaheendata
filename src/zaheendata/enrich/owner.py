"""Owner / decision-maker contact enrichment (step 4 of the method).

Precedence: vendor-supplied owner (Data Axle / D&B / ZoomInfo / import) -> Apollo people search
-> Hunter domain search -> owner name parsed from the website (+ site email/phone)."""

from __future__ import annotations

from typing import Any

from ..http import HttpClient
from ..logging import get_logger
from ..models import Business, OwnerContact
from ..settings import Settings, get_settings
from ..sources.apollo import ApolloSource

log = get_logger(__name__)

OWNER_TITLE_WORDS = ("owner", "founder", "president", "ceo", "principal", "proprietor", "partner")


class HunterClient:
    URL = "https://api.hunter.io/v2/domain-search"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.key = settings.secret("hunter_api_key")

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def find(self, domain: str) -> OwnerContact | None:
        if not self.key:
            return None
        try:
            data = await self.http.get_json(
                self.URL,
                params={"domain": domain, "api_key": self.key, "limit": 10},
                meter="hunter.domain_search",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("hunter.failed", domain=domain, error=str(exc))
            return None
        emails: list[dict[str, Any]] = (data.get("data") or {}).get("emails") or []
        if not emails:
            return None
        ranked = sorted(
            emails,
            key=lambda e: (
                0 if any(w in (e.get("position") or "").lower() for w in OWNER_TITLE_WORDS) else 1,
                -(e.get("confidence") or 0),
            ),
        )
        best = ranked[0]
        return OwnerContact(
            name=" ".join(filter(None, [best.get("first_name"), best.get("last_name")])) or None,
            title=best.get("position"),
            email=best.get("value"),
            phone=best.get("phone_number"),
            linkedin_url=best.get("linkedin"),
            source="hunter",
            confidence=min(0.9, (best.get("confidence") or 50) / 100),
        )


class OwnerEnricher:
    def __init__(
        self, http: HttpClient, settings: Settings | None = None, apollo: ApolloSource | None = None
    ):
        self.settings = settings or get_settings()
        self.http = http
        self.apollo = apollo
        self.hunter = HunterClient(http, self.settings)

    async def enrich(self, b: Business) -> OwnerContact:
        owner = b.owner.model_copy()
        if owner.name and owner.email:
            return owner

        if self.apollo is not None and (b.domain or b.name):
            try:
                found = await self.apollo.find_owner(b.domain, b.name)
            except Exception as exc:  # noqa: BLE001
                log.warning("apollo.owner_failed", business=b.name, error=str(exc))
                found = None
            if found:
                owner = self._merge(owner, found)
        if (not owner.email) and b.domain and self.hunter.available:
            found = await self.hunter.find(b.domain)
            if found:
                owner = self._merge(owner, found)

        wf = b.website_features
        if not owner.name and wf.owner_name_candidates:
            owner.name = wf.owner_name_candidates[0]
            owner.title = owner.title or "Owner (from website)"
            owner.source = owner.source or "website"
            owner.confidence = max(owner.confidence, 0.4)
        if not owner.email and wf.emails:
            personal = [
                e
                for e in wf.emails
                if e.split("@")[0]
                not in (
                    "info",
                    "contact",
                    "hello",
                    "office",
                    "sales",
                    "support",
                    "admin",
                    "booking",
                    "appointments",
                )
            ]
            owner.email = (personal or wf.emails)[0]
            owner.source = owner.source or "website"
            owner.confidence = max(owner.confidence, 0.3)
        if not owner.phone and b.phone_e164:
            owner.phone = b.phone_e164
        site_linkedin = wf.social_links.get("linkedin")
        if not owner.linkedin_url and site_linkedin and "/in/" in site_linkedin:
            owner.linkedin_url = site_linkedin  # personal profile only; company pages are not a contact
        return owner

    @staticmethod
    def _merge(base: OwnerContact, found: OwnerContact) -> OwnerContact:
        return OwnerContact(
            name=base.name or found.name,
            title=base.title or found.title,
            email=base.email or found.email,
            phone=base.phone or found.phone,
            linkedin_url=base.linkedin_url or found.linkedin_url,
            source=base.source or found.source,
            confidence=max(base.confidence, found.confidence),
        )
