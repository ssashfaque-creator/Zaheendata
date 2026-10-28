"""Canonicalisation helpers used by entity resolution and filters."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import phonenumbers

from .models import Address, RawRecord

_LEGAL_SUFFIXES = re.compile(
    r"\b(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|company|pllc|plc|lp|llp|dba)\b\.?",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^a-z0-9& ]+")
_WS = re.compile(r"\s+")
_GENERIC_WORDS = {"the", "and", "of", "a", "an", "&"}

_SOCIAL_HOSTS = {
    "facebook.com",
    "instagram.com",
    "yelp.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "google.com",
    "business.site",
    "linktr.ee",
    "nextdoor.com",
    "angi.com",
    "houzz.com",
    "thumbtack.com",
}


def normalize_phone(raw: str | None, region: str = "US") -> str | None:
    if not raw:
        return None
    try:
        parsed = phonenumbers.parse(raw, region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "http://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if not host or "." not in host:
        return None
    path = parts.path or "/"
    return urlunsplit((parts.scheme.lower(), host, path, parts.query, ""))


def extract_domain(url: str | None) -> str | None:
    url = normalize_url(url)
    if not url:
        return None
    host = urlsplit(url).netloc.split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host or None


def is_social_or_directory_url(url: str | None) -> bool:
    domain = extract_domain(url)
    if not domain:
        return False
    return any(domain == h or domain.endswith("." + h) for h in _SOCIAL_HOSTS)


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    text = name.lower().replace("’", "'").replace("'", "")
    text = _LEGAL_SUFFIXES.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    tokens = [t for t in _WS.split(text) if t and t not in _GENERIC_WORDS]
    return " ".join(tokens)


_STATE_RE = re.compile(r"\b([A-Z]{2})\b\s+(\d{5})(?:-\d{4})?")


def normalize_postal_code(code: str | None) -> str | None:
    if not code:
        return None
    m = re.search(r"\d{5}", code)
    return m.group(0) if m else code.strip() or None


def infer_region_from_formatted(formatted: str | None) -> tuple[str | None, str | None]:
    """Return (state_code, zip) parsed from a US formatted address when possible."""
    if not formatted:
        return None, None
    m = _STATE_RE.search(formatted)
    if m:
        return m.group(1), m.group(2)
    return None, None


def complete_address(address: Address | None) -> Address | None:
    if address is None:
        return None
    region, postal = infer_region_from_formatted(address.formatted)
    return address.model_copy(
        update={
            "region": (address.region or region or None),
            "postal_code": normalize_postal_code(address.postal_code or postal),
        }
    )


def normalize_record(record: RawRecord) -> RawRecord:
    """Return a copy of the record with canonical phone / website / address fields."""
    website = normalize_url(record.website)
    if website and is_social_or_directory_url(website):
        record.attributes.setdefault("social_profile_url", website)
        website = None
    return record.model_copy(
        update={
            "name": " ".join(record.name.split()),
            "phone": normalize_phone(
                record.phone, (record.address.country if record.address else None) or "US"
            ),
            "website": website,
            "address": complete_address(record.address),
            "owner_email": record.owner_email.lower().strip() if record.owner_email else None,
        }
    )
