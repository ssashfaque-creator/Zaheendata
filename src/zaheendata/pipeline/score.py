from __future__ import annotations

from zaheendata.models import Lead
from zaheendata.normalize import looks_like_person
from zaheendata.subsectors import Subsector

def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes"}


def score_busy(lead: Lead) -> tuple[int, str]:
    reviews = _int(lead.google_reviews)
    recent = _int(lead.recent_reviews_90d)
    if reviews >= 50 and recent > 0:
        return 2, f"{reviews} Google reviews with {recent} in last 90 days"
    if reviews >= 50:
        return 1, f"{reviews} Google reviews; none of the sampled reviews are recent"
    return 0, f"{reviews} Google reviews; below 50"


def score_admin_pain(lead: Lead) -> tuple[int, str]:
    review_pain = bool(lead.pain_examples)
    no_intake = (not _truthy(lead.online_booking)) and (not _truthy(lead.quote_form))
    call_only = _truthy(lead.says_call_for_quote) and no_intake
    if review_pain and call_only:
        return 2, f"review pain ({lead.pain_examples}); call-for-quote with no form"
    if review_pain:
        return 1, f"review pain: {lead.pain_examples}"
    if call_only:
        return 1, "site says call for a quote and has no booking widget or form"
    if _truthy(lead.says_call_for_quote):
        return 0, "mentions calling for a quote but already has a form or booking widget"
    return 0, "no pain signals found"


def score_multi_tool(lead: Lead) -> tuple[int, str]:
    categories = [part for part in (lead.tool_categories or "").split(",") if part]
    tools = lead.tech_stack or "none"
    if len(categories) >= 3:
        return 2, f"{len(categories)} tool categories: {lead.tool_categories}; tools: {tools}"
    if len(categories) == 2:
        return 1, f"2 tool categories: {lead.tool_categories}; tools: {tools}"
    return 0, f"{len(categories)} tool categories: {lead.tool_categories or 'none'}; tools: {tools}"


def score_owner(lead: Lead) -> tuple[int, str]:
    person = looks_like_person(lead.owner_name)
    has_email = bool(lead.owner_email)
    if person and has_email:
        return 2, f"owner named {lead.owner_name} with email {lead.owner_email} ({lead.owner_source or 'unknown'})"
    if person:
        return 1, f"owner named: {lead.owner_name} ({lead.owner_source or 'unknown'})"
    if has_email:
        return 1, f"email found ({lead.owner_email}) but owner name is not a person"
    return 0, "owner not identified"


def score_subsector(lead: Lead, subsector: Subsector) -> tuple[int, str]:
    matched: list[str] = []
    keywords = subsector.signal_keywords
    if subsector.key == "plumbing_hvac":
        if _truthy(lead.emergency_service):
            matched.append("emergency_service")
        if _truthy(lead.maintenance_plan):
            matched.append("maintenance_plan")
        score = 2 if len(matched) >= 2 else 1 if matched else 0
        return score, f"{subsector.fit_signal}; matched: {', '.join(matched) or 'none'}"
    if "recurring_service" in keywords and _truthy(lead.recurring_service):
        matched.append("recurring_service")
    if "free_estimate" in keywords and _truthy(lead.free_estimate):
        matched.append("free_estimate")
    if "inspection" in keywords and _truthy(lead.quote_form):
        matched.append("inspection/quote form")
    if "gift_cards" in keywords and _truthy(lead.sells_gift_cards):
        matched.append("gift_cards")
    if "hosts_events" in keywords and _truthy(lead.hosts_events):
        matched.append("hosts_events")
    if "online_store" in keywords and _truthy(lead.online_store):
        matched.append("online_store")
    if "instagram" in keywords and lead.instagram:
        matched.append("instagram")
    if "grooming" in keywords and "groom" in (lead.score_evidence or "").lower():
        matched.append("grooming")
    score = 2 if len(matched) >= 2 else 1 if matched else 0
    return score, f"{subsector.fit_signal}; matched: {', '.join(matched) or 'none'}"


def score_lead(lead: Lead, subsector: Subsector) -> Lead:
    busy, busy_why = score_busy(lead)
    pain, pain_why = score_admin_pain(lead)
    tools, tools_why = score_multi_tool(lead)
    owner, owner_why = score_owner(lead)
    signal, signal_why = score_subsector(lead, subsector)
    lead.busy_and_growing = busy
    lead.admin_pain = pain
    lead.multi_tool = tools
    lead.owner_reachable = owner
    lead.subsector_signal = signal
    lead.score_total = busy + pain + tools + owner + signal
    lead.score_evidence = (
        f"busy_and_growing={busy}: {busy_why} | "
        f"admin_pain={pain}: {pain_why} | "
        f"multi_tool={tools}: {tools_why} | "
        f"owner_reachable={owner}: {owner_why} | "
        f"subsector_signal={signal}: {signal_why}"
    )
    if lead.status not in {"rejected"}:
        lead.status = "scored"
    return lead


def rank_leads(leads: list[Lead], keep: int = 100) -> list[Lead]:
    scored = [lead for lead in leads if lead.status != "rejected"]
    scored.sort(
        key=lambda lead: (
            _int(lead.score_total),
            _int(lead.google_reviews),
            1 if lead.website else 0,
            1 if lead.owner_email else 0,
        ),
        reverse=True,
    )
    for index, lead in enumerate(scored, start=1):
        lead.rank = index
        lead.status = "selected" if index <= keep else "scored"
    rejected = [lead for lead in leads if lead.status == "rejected"]
    return scored + rejected
