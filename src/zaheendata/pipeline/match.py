from __future__ import annotations

from zaheendata.models import Lead
from zaheendata.normalize import looks_like_person, name_key, name_similarity, phone_key

MATCH_THRESHOLD = 0.72


def attach_licenses(leads: list[Lead], licenses: list[dict]) -> list[Lead]:
    by_phone: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for record in licenses:
        phone = phone_key(record.get("phone"))
        if phone:
            by_phone.setdefault(phone, record)
        key = name_key(record.get("business_name"))
        if key:
            by_name.setdefault(key, record)

    for lead in leads:
        matched = None
        phone = phone_key(lead.phone)
        if phone and phone in by_phone:
            matched = by_phone[phone]
        if matched is None:
            lead_key = name_key(lead.name)
            if lead_key in by_name:
                matched = by_name[lead_key]
        if matched is None:
            best = (0.0, None)
            for record in licenses:
                score = name_similarity(lead.name, record.get("business_name"))
                if score > best[0]:
                    best = (score, record)
            if best[0] >= MATCH_THRESHOLD:
                matched = best[1]
        if not matched:
            continue
        lead.merge_source(matched.get("source") or "license")
        if matched.get("license_number"):
            lead.license_number = matched["license_number"]
        if matched.get("license_status"):
            lead.license_status = matched["license_status"]
        owner = (matched.get("owner_name") or "").strip()
        if owner and (matched.get("owner_is_person") or looks_like_person(owner)):
            lead.owner_name = owner
            lead.owner_source = matched.get("source") or "license"
            lead.owner_confidence = round(max(float(lead.owner_confidence or 0), 0.8), 2)
            lead.owner_title = lead.owner_title or "Owner / license holder"
        if matched.get("phone") and not lead.phone:
            lead.phone = matched["phone"]
        lead.extra["license_match"] = matched.get("business_name")
    return leads
