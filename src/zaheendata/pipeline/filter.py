from __future__ import annotations

from zaheendata.franchises import franchise_hit
from zaheendata.models import Lead
from zaheendata.normalize import is_own_website
from zaheendata.subsectors import Subsector

OFF_TAXONOMY = {
    "electrical": ("semiconductor", "nxp", "fence", "tree tamer", "tree service", "irrigation"),
    "plumbing_hvac": ("semiconductor", "solar panel distributor", "johnstone"),
}


def _review_count(lead: Lead) -> int:
    try:
        return int(lead.google_reviews or 0)
    except (TypeError, ValueError):
        return 0


def classify(
    lead: Lead,
    subsector: Subsector,
    *,
    require_website: bool = True,
    require_reviews: bool = True,
) -> Lead:
    brand = franchise_hit(lead.name) or franchise_hit(lead.owner_name)
    if brand:
        lead.status = "rejected"
        lead.rejection_reason = f"franchise/chain: franchise brand: {brand}"
        return lead

    haystack = f"{lead.name} {lead.categories}".lower()
    for token in OFF_TAXONOMY.get(subsector.key, ()):
        if token in haystack:
            lead.status = "rejected"
            lead.rejection_reason = f"off-taxonomy: {token}"
            return lead

    if lead.website and not is_own_website(lead.website):
        lead.status = "rejected"
        lead.rejection_reason = "directory listing, not own website"
        return lead

    if require_website and not lead.website:
        lead.status = "rejected"
        lead.rejection_reason = "no website"
        return lead

    reviews = _review_count(lead)
    if require_reviews and lead.google_place_id and reviews < subsector.min_reviews_keep:
        lead.status = "rejected"
        lead.rejection_reason = f"too few Google reviews: {reviews} < {subsector.min_reviews_keep}"
        return lead

    lead.status = "enrich"
    return lead


def keep_for_enrichment(
    leads: list[Lead],
    subsector: Subsector,
    *,
    require_website: bool = True,
    require_reviews: bool = True,
) -> list[Lead]:
    kept: list[Lead] = []
    for lead in leads:
        classify(
            lead,
            subsector,
            require_website=require_website,
            require_reviews=require_reviews,
        )
        if lead.status != "rejected":
            kept.append(lead)
    return kept
