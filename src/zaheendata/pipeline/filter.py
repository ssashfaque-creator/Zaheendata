from __future__ import annotations

from zaheendata.franchises import franchise_hit
from zaheendata.models import Lead
from zaheendata.subsectors import Subsector


def _review_count(lead: Lead) -> int:
    try:
        return int(lead.google_reviews or 0)
    except (TypeError, ValueError):
        return 0


def classify(lead: Lead, subsector: Subsector, *, require_website: bool = True) -> Lead:
    brand = franchise_hit(lead.name) or franchise_hit(lead.owner_name)
    if brand:
        lead.status = "rejected"
        lead.rejection_reason = f"franchise/chain: franchise brand: {brand}"
        return lead

    if require_website and not lead.website:
        lead.status = "rejected"
        lead.rejection_reason = "no website"
        return lead

    reviews = _review_count(lead)
    if lead.google_place_id and reviews < subsector.min_reviews_keep:
        lead.status = "rejected"
        lead.rejection_reason = f"too few Google reviews: {reviews} < {subsector.min_reviews_keep}"
        return lead

    lead.status = "enrich"
    return lead


def keep_for_enrichment(leads: list[Lead], subsector: Subsector) -> list[Lead]:
    kept: list[Lead] = []
    for lead in leads:
        classify(lead, subsector)
        if lead.status != "rejected":
            kept.append(lead)
    return kept
