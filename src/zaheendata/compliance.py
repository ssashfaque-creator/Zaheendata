"""Outreach-compliance helpers.

The engine builds research lists; it never sends email or dials. These helpers make the export
safe to hand to an outreach team: suppression lists (opt-outs, DNC scrubs, existing customers),
role-account flags and the consent-basis columns CAN-SPAM / TCPA reviews expect."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Business

ROLE_ACCOUNTS = {
    "info",
    "contact",
    "hello",
    "office",
    "sales",
    "support",
    "admin",
    "booking",
    "appointments",
    "team",
    "help",
    "service",
    "billing",
    "frontdesk",
    "reception",
    "mail",
    "noreply",
    "no-reply",
}
FREE_MAIL = {
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "aol.com",
    "icloud.com",
    "me.com",
    "live.com",
}


@dataclass
class ComplianceFlags:
    suppressed: bool = False
    suppression_reasons: list[str] = field(default_factory=list)
    email_is_role_account: bool = False
    email_is_free_mail: bool = False
    email_channel: str = (
        "CAN-SPAM: commercial email permitted with opt-out, accurate header, physical address"
    )
    phone_channel: str = (
        "TCPA: manual dial only; no autodialer/prerecorded/SMS without prior express consent; scrub DNC"
    )
    data_sources: list[str] = field(default_factory=list)


def email_domain(email: str | None) -> str | None:
    return email.split("@", 1)[1].lower() if email and "@" in email else None


def evaluate(b: Business, suppressions: dict[str, set[str]] | None = None) -> ComplianceFlags:
    flags = ComplianceFlags(data_sources=b.sources)
    sup = suppressions or {}
    email = (b.owner.email or "").lower()
    if email:
        local = email.split("@")[0]
        flags.email_is_role_account = local in ROLE_ACCOUNTS or bool(
            re.match(r"^(info|contact|sales)\d*$", local)
        )
        flags.email_is_free_mail = (email_domain(email) or "") in FREE_MAIL
        if email in sup.get("email", set()):
            flags.suppressed = True
            flags.suppression_reasons.append("email suppressed")
    for phone in {b.phone_e164, b.owner.phone}:
        if phone and phone.lower() in sup.get("phone", set()):
            flags.suppressed = True
            flags.suppression_reasons.append("phone suppressed")
    for dom in {b.domain, email_domain(email)}:
        if dom and dom in sup.get("domain", set()):
            flags.suppressed = True
            flags.suppression_reasons.append(f"domain suppressed: {dom}")
    return flags
