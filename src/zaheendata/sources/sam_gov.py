"""SAM.gov Entity Management API (free key from api.data.gov).

Every business registered to do work with the US government is listed with its NAICS codes,
physical address, website, entity structure and points of contact. Small service firms
(janitorial, landscaping, painting, electrical, HVAC, pest control) register often, which makes
this a free, NAICS-filterable long-list source that also names a decision maker.
Public keys return POC names and titles; email/phone require a FOUO-entitled key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from rapidfuzz import fuzz

from ..logging import get_logger
from ..models import Address, OwnerContact, RawRecord, SourceKind, SourceSearchSpec
from ..normalize import normalize_name
from ..settings import Settings
from .base import Source

log = get_logger(__name__)

ENTITIES_URL = "https://api.sam.gov/entity-information/v3/entities"
PAGE_SIZE = 10
OWNER_TITLE_WORDS = ("owner", "president", "ceo", "founder", "principal", "member", "partner", "proprietor")


def _poc(entity: dict[str, Any]) -> OwnerContact | None:
    pocs = entity.get("pointsOfContact") or {}
    candidates: list[dict[str, Any]] = [
        c
        for c in (
            pocs.get(k) for k in ("governmentBusinessPOC", "electronicBusinessPOC", "pastPerformancePOC")
        )
        if isinstance(c, dict) and (c.get("firstName") or c.get("lastName"))
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda c: 0 if any(w in (c.get("title") or "").lower() for w in OWNER_TITLE_WORDS) else 1
    )
    c = candidates[0]
    return OwnerContact(
        name=" ".join(filter(None, [c.get("firstName"), c.get("lastName")])).title(),
        title=c.get("title"),
        email=(c.get("email") or None),
        phone=(c.get("usPhone") or c.get("phone") or None),
        source="sam_gov",
        confidence=0.6 if c.get("email") else 0.5,
    )


def parse_entity(entity: dict[str, Any], naics_hint: str | None = None) -> RawRecord | None:
    reg = entity.get("entityRegistration") or {}
    core = entity.get("coreData") or {}
    addr = core.get("physicalAddress") or {}
    info = core.get("entityInformation") or {}
    general = core.get("generalInformation") or {}
    naics_list = ((entity.get("assertions") or {}).get("goodsAndServices") or {}).get("naicsList") or []
    name = reg.get("dbaName") or reg.get("legalBusinessName")
    if not name:
        return None
    naics_codes = [str(n.get("naicsCode")) for n in naics_list if n.get("naicsCode")]
    primary = next(
        (n for n in naics_list if str(n.get("sbaSmallBusiness", "")).upper() in ("Y", "YES")), None
    )
    owner = _poc(entity)
    return RawRecord(
        source="sam_gov",
        source_kind=SourceKind.REGISTRY,
        source_id=reg.get("ueiSAM"),
        url=f"https://sam.gov/entity/{reg.get('ueiSAM')}" if reg.get("ueiSAM") else None,
        name=str(name).strip(),
        website=info.get("entityURL"),
        address=Address(
            line1=addr.get("addressLine1"),
            city=addr.get("city"),
            region=addr.get("stateOrProvinceCode"),
            postal_code=addr.get("zipCode"),
            country=addr.get("countryCode") or "US",
        ),
        categories=[n.get("naicsDescription") for n in naics_list[:5] if n.get("naicsDescription")],
        naics=(naics_hint if naics_hint in naics_codes else None)
        or (primary or {}).get("naicsCode")
        or (naics_codes[0] if naics_codes else None),
        owner_name=owner.name if owner else None,
        owner_title=owner.title if owner else None,
        owner_email=owner.email if owner else None,
        owner_phone=owner.phone if owner else None,
        attributes={
            "legal_name": reg.get("legalBusinessName"),
            "uei": reg.get("ueiSAM"),
            "cage_code": reg.get("cageCode"),
            "entity_structure": general.get("entityStructureDesc"),
            "registration_date": reg.get("registrationDate") or info.get("entityStartDate"),
            "naics_codes": naics_codes,
            "small_business": bool(primary),
        },
    )


class SamGovSource(Source):
    name = "sam_gov"
    kind = SourceKind.REGISTRY
    tags: ClassVar[set[str]] = {"sam_gov", "state_business_registration"}
    description = (
        "SAM.gov federal entity registrations: NAICS, address, website, entity structure, POC names."
    )
    trust = 65
    always_on = True

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return settings.sam_gov_api_key is not None

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "set ZAHEEN_SAM_GOV_API_KEY (free at api.data.gov)"

    def _params(self, **extra: Any) -> dict[str, Any]:
        self.http.set_host_rate("api.sam.gov", 1.0, concurrency=2)
        return {
            "api_key": self.settings.secret("sam_gov_api_key"),
            "registrationStatus": "A",
            "size": PAGE_SIZE,
            **extra,
        }

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        if not spec.region:
            return
        params = self._params(naicsCode=spec.naics, physicalAddressProvinceOrStateCode=spec.region)
        if spec.location_text and "," in spec.location_text:
            params["physicalAddressCity"] = spec.location_text.split(",")[0].strip().upper()
        page = 0
        seen = 0
        while True:
            data = await self.http.get_json(
                ENTITIES_URL, params={**params, "page": page}, meter="sam_gov.entities"
            )
            entities = data.get("entityData") or []
            for e in entities:
                rec = parse_entity(e, spec.naics)
                if rec:
                    seen += 1
                    yield rec
            total = int(data.get("totalRecords") or 0)
            page += 1
            if not entities or page * PAGE_SIZE >= total or seen >= spec.limit * 3:
                break

    async def find_entity(self, business_name: str, region: str | None) -> RawRecord | None:
        """Look a business up by legal / DBA name (website, address, POC)."""
        target = normalize_name(business_name)
        if not target:
            return None
        for field in ("legalBusinessName", "dbaName"):
            params = self._params(**{field: business_name})
            if region:
                params["physicalAddressProvinceOrStateCode"] = region
            try:
                data = await self.http.get_json(ENTITIES_URL, params=params, meter="sam_gov.entities")
            except Exception as exc:  # noqa: BLE001
                log.debug("sam_gov.lookup_failed", name=business_name, error=str(exc)[:200])
                return None
            for e in data.get("entityData") or []:
                rec = parse_entity(e)
                if rec and fuzz.token_sort_ratio(target, normalize_name(rec.name)) >= 88:
                    return rec
        return None

    async def find_owner(self, business_name: str, region: str | None) -> OwnerContact | None:
        """Look a shortlisted business up by legal / DBA name for its point of contact."""
        rec = await self.find_entity(business_name, region)
        if rec is None:
            return None
        if rec.owner_name:
            return OwnerContact(
                name=rec.owner_name,
                title=rec.owner_title,
                email=rec.owner_email,
                phone=rec.owner_phone,
                source="sam_gov",
                confidence=0.6 if rec.owner_email else 0.5,
            )
        return None
