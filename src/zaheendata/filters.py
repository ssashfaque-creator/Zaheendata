"""Step 2 of the method: remove poor fits.

Drops franchises and chains, businesses with no website, businesses with fewer than N Google
reviews, permanently closed businesses, anything outside the target area and (optionally)
businesses outside the 5-19 employee band when a vendor supplied a head-count."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .geo import AreaMatcher
from .models import Business, BusinessStatus
from .settings import Settings, get_settings


@dataclass
class FranchiseConfig:
    brands: list[str]
    domains: set[str]
    min_locations_for_chain: int = 4
    _brand_re: re.Pattern[str] = field(init=False)

    def __post_init__(self) -> None:
        escaped = sorted((re.escape(b.lower()) for b in self.brands), key=len, reverse=True)
        self._brand_re = (
            re.compile(r"(?<![a-z0-9])(" + "|".join(escaped) + r")(?![a-z0-9])")
            if escaped
            else re.compile(r"$^")
        )

    def brand_match(self, name: str) -> str | None:
        m = self._brand_re.search(name.lower().replace("’", "'"))
        return m.group(1) if m else None


@lru_cache
def load_franchise_config(path: Path | None = None) -> FranchiseConfig:
    path = path or get_settings().config_dir / "franchises.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    brands = [b for group in (data.get("brands") or {}).values() for b in group]
    return FranchiseConfig(
        brands=brands,
        domains={d.lower() for d in data.get("domains", [])},
        min_locations_for_chain=int(data.get("min_locations_for_chain", 4)),
    )


class FranchiseDetector:
    """Combines a brand list with structural signals seen across the whole candidate set."""

    def __init__(self, config: FranchiseConfig | None = None):
        self.config = config or load_franchise_config()
        self._domain_counts: Counter[str] = Counter()
        self._phone_counts: Counter[str] = Counter()

    def fit(self, businesses: list[Business]) -> None:
        self._domain_counts = Counter(b.domain for b in businesses if b.domain)
        self._phone_counts = Counter(b.phone_e164 for b in businesses if b.phone_e164)

    def reason(self, b: Business) -> str | None:
        brand = self.config.brand_match(b.name)
        if brand:
            return f"franchise brand: {brand}"
        if b.domain and (
            b.domain in self.config.domains or any(b.domain.endswith("." + d) for d in self.config.domains)
        ):
            return f"franchise domain: {b.domain}"
        flag = b.attributes.get("franchise_flag") or b.attributes.get("chain_flag")
        if isinstance(flag, str) and flag.strip().lower() in {"y", "yes", "true", "1", "franchise"}:
            return "vendor franchise flag"
        if flag is True:
            return "vendor franchise flag"
        if b.attributes.get("brand_wikidata"):
            return f"OSM brand tag: {b.attributes.get('brand')}"
        threshold = self.config.min_locations_for_chain
        if b.domain and self._domain_counts[b.domain] >= threshold:
            return f"domain shared by {self._domain_counts[b.domain]} locations"
        if b.phone_e164 and self._phone_counts[b.phone_e164] >= threshold:
            return f"phone shared by {self._phone_counts[b.phone_e164]} locations"
        wf = b.website_features
        if wf.franchise_language and not wf.locally_owned_language:
            return "website franchise language"
        if wf.location_count_hint and wf.location_count_hint >= threshold:
            return f"website lists {wf.location_count_hint} locations"
        return None


class PoorFitFilter:
    def __init__(
        self,
        area: AreaMatcher | None,
        settings: Settings | None = None,
        franchise: FranchiseDetector | None = None,
    ):
        self.area = area
        self.settings = settings or get_settings()
        self.franchise = franchise or FranchiseDetector()

    def apply(
        self, businesses: list[Business], require_website: bool = True, require_reviews: bool = True
    ) -> dict[str, int]:
        self.franchise.fit(businesses)
        reasons: Counter[str] = Counter()
        for b in businesses:
            reason = self.reject_reason(b, require_website=require_website, require_reviews=require_reviews)
            if reason:
                b.status = BusinessStatus.REJECTED
                b.rejection_reason = reason
                reasons[reason.split(":")[0]] += 1
            elif b.status == BusinessStatus.REJECTED:
                b.status = BusinessStatus.CANDIDATE
                b.rejection_reason = None
        return dict(reasons)

    def reject_reason(
        self, b: Business, require_website: bool = True, require_reviews: bool = True
    ) -> str | None:
        s = self.settings
        if b.business_status and b.business_status.upper() in {"CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"}:
            return f"closed: {b.business_status}"
        fr = self.franchise.reason(b)
        if fr:
            return f"franchise/chain: {fr}"
        if require_website and not b.website:
            return "no website"
        if require_reviews and (b.review_count or 0) < s.min_google_reviews:
            return f"too few reviews: {b.review_count or 0} < {s.min_google_reviews}"
        if self.area is not None:
            region = b.address.region if b.address else None
            inside, why = self.area.inside(b.location, region)
            if not inside:
                return f"outside target area: {why}"
        if (
            s.enforce_employee_range
            and b.employee_count is not None
            and not (s.employee_min <= b.employee_count <= s.employee_max)
        ):
            return f"employee count out of range: {b.employee_count}"
        return None
