"""Owner / decision-maker contact enrichment (step 4 of the method) using free sources only.

Waterfall:
  1. owner already carried by a registry / licence / import record (licence holder, SF ownership
     name, CT registry principal, SAM.gov point of contact);
  2. SAM.gov entity lookup by legal / DBA name (POC name + title; email/phone with a FOUO key);
  3. Secretary of State registry lookup (officer, CEO or registered-agent name);
  4. the business website: owner byline ("Owner: Jane Doe"), a non-role email, a personal LinkedIn
     profile; the business phone is always kept as the fallback phone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..http import HttpClient
from ..logging import get_logger
from ..models import Business, OwnerContact
from ..settings import Settings, get_settings

log = get_logger(__name__)

OWNER_TITLE_WORDS = ("owner", "founder", "president", "ceo", "principal", "proprietor", "partner")
ROLE_MAILBOXES = frozenset(
    {
        "info",
        "contact",
        "hello",
        "office",
        "sales",
        "support",
        "admin",
        "booking",
        "bookings",
        "appointments",
        "service",
        "team",
        "mail",
        "noreply",
        "no-reply",
    }
)

OwnerLookup = Callable[[str, str | None], Awaitable[OwnerContact | None]]


class OwnerEnricher:
    def __init__(
        self,
        http: HttpClient,
        settings: Settings | None = None,
        lookups: list[tuple[str, OwnerLookup]] | None = None,
    ):
        """``lookups`` are ``(label, async fn(business_name, state) -> OwnerContact | None)`` pairs
        tried in order (e.g. SAM.gov, then Secretary of State registries)."""
        self.settings = settings or get_settings()
        self.http = http
        self.lookups = lookups or []

    async def enrich(self, b: Business) -> OwnerContact:
        owner = b.owner.model_copy()
        region = b.address.region if b.address else None

        if not (owner.name and owner.email):
            for label, lookup in self.lookups:
                try:
                    found = await lookup(b.name, region)
                except Exception as exc:  # noqa: BLE001 - a lookup failing must not break the stage
                    log.warning("owner.lookup_failed", lookup=label, business=b.name, error=str(exc)[:200])
                    continue
                if found and found.name:
                    owner = self._merge(owner, found)
                    if owner.name and owner.email:
                        break

        wf = b.website_features
        if not owner.name and wf.owner_name_candidates:
            owner.name = wf.owner_name_candidates[0]
            owner.title = owner.title or "Owner (from website)"
            owner.source = owner.source or "website"
            owner.confidence = max(owner.confidence, 0.4)
        if not owner.email and wf.emails:
            personal = [e for e in wf.emails if e.split("@")[0].lower() not in ROLE_MAILBOXES]
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
