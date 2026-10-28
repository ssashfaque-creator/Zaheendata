"""Step 3 of the method: score each surviving business on five fit signals (0, 1 or 2 points).

  busy_and_growing   50+ Google reviews, with recent ones
  admin_pain         reviews mention slow callbacks / billing problems; site says "call for a
                     quote" with no online booking or form
  multi_tool         Wappalyzer-style fingerprinting shows a mix of separate tools
  owner_reachable    owner named on the site, LinkedIn or Google profile (+ direct contact)
  subsector_signal   the extra signal for that sub-sector (rules in config/subsectors.yaml)

Total is 0-10. Step 4 keeps the top N.
"""

from __future__ import annotations

from collections.abc import Callable

from .compliance import ROLE_ACCOUNTS
from .models import Business, BusinessStatus, FitScore, SignalScore
from .registry import SubSector
from .settings import Settings, get_settings

# Tool categories that count as "separate tools" (analytics / CMS are not business tools).
TOOL_CATEGORIES = {
    "payments",
    "ecommerce",
    "booking",
    "field_service",
    "email_marketing",
    "crm",
    "reputation",
    "live_chat",
    "forms",
    "accounting",
    "gift_cards",
    "financing",
    "call_tracking",
    "events",
    "live_selling",
    "wholesale",
}


def feature_resolver(b: Business) -> Callable[[str], bool]:
    wf = b.website_features
    tech_cats = b.tech_stack.categories
    tech_names = {n.lower() for n in b.tech_stack.names}

    derived: dict[str, bool] = {
        "no_online_booking": wf.crawled and not wf.has_online_booking,
        "five_plus_chairs": (wf.chair_count_hint or 0) >= 5,
        "store_on_shopify_or_etsy": (wf.store_platform in {"shopify", "etsy"})
        or bool({"shopify", "etsy"} & tech_names),
        "instagram_present": "instagram" in wf.social_links or "instagram" in tech_names,
        "commercial_only": wf.commercial_focus and not wf.residential_focus,
        "wire_service_only": bool(
            b.domain and any(w in b.domain for w in ("ftd.com", "teleflora.com", "bloomnation.com"))
        ),
        "busy_50_reviews": (b.review_count or 0) >= 50,
    }

    def resolve(name: str) -> bool:
        if name in derived:
            return derived[name]
        if name.startswith("tech:"):
            return name[5:] in tech_cats or name[5:].lower() in tech_names
        if name.startswith("social:"):
            return name[7:] in wf.social_links
        if name.startswith("attr:"):
            return bool(b.attributes.get(name[5:]))
        value = getattr(wf, name, None)
        if isinstance(value, bool):
            return value
        if value is not None:
            return bool(value)
        return False

    return resolve


class ScoringEngine:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def score(self, b: Business, subsector: SubSector) -> FitScore:
        signals = [
            self._busy_and_growing(b),
            self._admin_pain(b),
            self._multi_tool(b),
            self._owner_reachable(b),
            self._subsector_signal(b, subsector),
        ]
        return FitScore(signals=signals, total=sum(s.points for s in signals))

    # ------------------------------------------------------------------ signals
    def _busy_and_growing(self, b: Business) -> SignalScore:
        s = self.settings
        count = b.review_count or 0
        recent = b.review_insights.recent_reviews
        ev = [f"{count} Google reviews"]
        if count < s.busy_reviews_threshold:
            return SignalScore(
                name="busy_and_growing", points=0, evidence=ev + [f"below {s.busy_reviews_threshold}"]
            )
        if b.review_insights.analysed_reviews == 0:
            # No review payload fetched: volume alone earns one point.
            return SignalScore(name="busy_and_growing", points=1, evidence=ev + ["recency unknown"])
        ev.append(f"{recent} in last {s.recent_review_window_days} days")
        if recent >= s.recent_reviews_min:
            return SignalScore(name="busy_and_growing", points=2, evidence=ev)
        return SignalScore(name="busy_and_growing", points=1, evidence=ev)

    def _admin_pain(self, b: Business) -> SignalScore:
        wf = b.website_features
        ri = b.review_insights
        ev: list[str] = []
        points = 0
        if ri.pain_mentions:
            points += 1
            cats = ", ".join(f"{k}x{v}" for k, v in ri.pain_categories.items())
            ev.append(f"{ri.pain_mentions} review(s) mention admin pain ({cats})")
            if ri.pain_mentions >= 2 and ri.analysed_reviews:
                points += 1
        no_digital_intake = wf.crawled and not wf.has_online_booking and not wf.has_quote_form
        if wf.says_call_for_quote and no_digital_intake:
            points += 1
            ev.append('website says "call for a quote" with no online booking or form')
        elif no_digital_intake and not wf.has_contact_form:
            points += 1
            ev.append("no online booking, quote form or contact form")
        return SignalScore(name="admin_pain", points=min(points, 2), evidence=ev or ["no pain signals found"])

    def _multi_tool(self, b: Business) -> SignalScore:
        techs = [t for t in b.tech_stack.technologies if t.category in TOOL_CATEGORIES]
        cats = {t.category for t in techs}
        if b.website_features.booking_provider and "booking" not in cats:
            cats.add("booking")
        if b.website_features.store_platform and "ecommerce" not in cats:
            cats.add("ecommerce")
        names = {t.name for t in techs}
        provider = b.website_features.booking_provider
        if provider and not any(provider.split(".")[0] in n.lower().replace(" ", "") for n in names):
            names.add(provider)
        ev = [
            f"{len(cats)} tool categories: {', '.join(sorted(cats)) or 'none'}",
            f"tools: {', '.join(sorted(names))}",
        ]
        points = 2 if len(cats) >= 3 else 1 if len(cats) == 2 else 0
        return SignalScore(name="multi_tool", points=points, evidence=ev)

    def _owner_reachable(self, b: Business) -> SignalScore:
        o = b.owner
        wf = b.website_features
        name = o.name or (wf.owner_name_candidates[0] if wf.owner_name_candidates else None)
        if not name:
            return SignalScore(name="owner_reachable", points=0, evidence=["owner not identified"])
        ev = [f"owner named: {name} ({o.source or 'website'})"]
        personal_email = o.email is not None and o.email.split("@")[0].lower() not in ROLE_ACCOUNTS
        direct_phone = bool(o.phone) and o.phone != b.phone_e164
        personal_linkedin = bool(o.linkedin_url) and "/in/" in (o.linkedin_url or "")
        direct = [
            k
            for k, v in (("email", personal_email), ("phone", direct_phone), ("linkedin", personal_linkedin))
            if v
        ]
        if direct:
            ev.append("direct contact: " + ", ".join(direct))
            return SignalScore(name="owner_reachable", points=2, evidence=ev)
        return SignalScore(name="owner_reachable", points=1, evidence=ev)

    def _subsector_signal(self, b: Business, subsector: SubSector) -> SignalScore:
        rule = subsector.fit_signal
        resolve = feature_resolver(b)
        matched = [f for f in rule.features if resolve(f)]
        negatives = [f for f in rule.negative_features if resolve(f)]
        ev = [rule.description, f"matched: {', '.join(matched) or 'none'}"]
        if negatives:
            ev.append(f"negative: {', '.join(negatives)}")
        if negatives and not matched:
            return SignalScore(name="subsector_signal", points=0, evidence=ev)
        if len(matched) >= rule.strong_threshold:
            points = 2
        elif matched:
            points = 1
        else:
            points = 0
        if negatives and points == 2:
            points = 1
        return SignalScore(name="subsector_signal", points=points, evidence=ev)


def rank_and_select(businesses: list[Business], top_n: int) -> list[Business]:
    """Rank scored businesses; mark the top N as SELECTED and the rest as SCORED."""
    scored = [b for b in businesses if b.status != BusinessStatus.REJECTED and b.score is not None]
    scored.sort(key=lambda b: (-(b.score.total if b.score else 0), -(b.review_count or 0), b.name.lower()))
    for i, b in enumerate(scored, start=1):
        assert b.score is not None
        b.score.rank = i
        b.status = BusinessStatus.SELECTED if i <= top_n else BusinessStatus.SCORED
    return scored[:top_n]
