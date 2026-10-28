from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

LEGAL_SUFFIXES = (
    "incorporated",
    "corporation",
    "company",
    "limited",
    "llc",
    "l.l.c",
    "l l c",
    "inc",
    "corp",
    "ltd",
    "lp",
    "l.p",
    "pllc",
    "pc",
    "co",
    "dba",
)

COMPANY_HINT = re.compile(
    r"\b(llc|l\.l\.c|inc|incorporated|corp|corporation|ltd|limited|lp|l\.p|"
    r"company|co|pllc|pc|holdings|services|group|enterprises)\b",
    re.I,
)

NON_ALNUM = re.compile(r"[^a-z0-9]+")
PHONE_DIGITS = re.compile(r"\D+")
ROLE_LOCALS = {
    "info",
    "office",
    "admin",
    "contact",
    "hello",
    "sales",
    "support",
    "service",
    "billing",
    "dispatch",
    "estimating",
    "estimate",
    "quotes",
    "quote",
}


def fold(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.strip()


def normalize_name(value: str | None) -> str:
    text = fold(value).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[\"'`’]", "", text)
    text = NON_ALNUM.sub(" ", text)
    tokens = [token for token in text.split() if token and token not in LEGAL_SUFFIXES]
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def name_key(value: str | None) -> str:
    return normalize_name(value).replace(" ", "")


def looks_like_company(value: str | None) -> bool:
    text = fold(value)
    if not text:
        return False
    if COMPANY_HINT.search(text):
        return True
    if "," in text:
        return False
    return text.isupper() and len(text.split()) >= 3


PERSON_STOPWORDS = {
    "at",
    "the",
    "our",
    "your",
    "this",
    "meet",
    "team",
    "owner",
    "support",
    "filters",
    "warranty",
    "services",
    "service",
}


def looks_like_person(value: str | None) -> bool:
    text = fold(value)
    if not text or looks_like_company(text):
        return False
    if "," in text:
        parts = [part.strip() for part in text.split(",") if part.strip()]
        return len(parts) >= 2 and all(part.replace(".", "").replace("-", " ").replace(" ", "").isalpha() for part in parts[:2])
    tokens = [token for token in re.split(r"\s+", text) if token]
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    if any(token.lower() in PERSON_STOPWORDS for token in tokens):
        return False
    return all(token.replace(".", "").replace("-", "").isalpha() for token in tokens)


def person_display_name(value: str | None) -> str:
    text = fold(value)
    if "," in text:
        last, first = [part.strip() for part in text.split(",", 1)]
        return f"{first} {last}".strip()
    return text


def normalize_phone(value: str | None) -> str:
    digits = PHONE_DIGITS.sub("", value or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return ""
    return "+1" + digits


def phone_key(value: str | None) -> str:
    return normalize_phone(value).lstrip("+")


DIRECTORY_HOSTS = {
    "homeadvisor.com",
    "angi.com",
    "angieslist.com",
    "yelp.com",
    "yellowpages.com",
    "bbb.org",
    "facebook.com",
    "instagram.com",
    "m.facebook.com",
    "mapquest.com",
    "nextdoor.com",
    "thumbtack.com",
    "houzz.com",
    "porch.com",
    "superpages.com",
    "whitepages.com",
    "google.com",
    "business.site",
    "n49.com",
}


def domain_from_url(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = urlparse(raw).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def is_own_website(value: str | None) -> bool:
    host = domain_from_url(value)
    if not host:
        return False
    return not any(host == blocked or host.endswith("." + blocked) for blocked in DIRECTORY_HOSTS)


def canonicalize_website(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = "https:" + raw
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc:
        return ""
    scheme = "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return f"{scheme}://{netloc}{path}/"


def email_is_role_account(email: str | None) -> bool:
    local = (email or "").split("@", 1)[0].lower()
    return local in ROLE_LOCALS or local.endswith("office")


def token_set(value: str | None) -> set[str]:
    return {token for token in normalize_name(value).split() if len(token) > 1}


def name_similarity(left: str | None, right: str | None) -> float:
    a = token_set(left)
    b = token_set(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    overlap = len(a & b) / len(a | b)
    compact_a, compact_b = "".join(sorted(a)), "".join(sorted(b))
    if compact_a in compact_b or compact_b in compact_a:
        overlap = max(overlap, 0.86)
    return round(overlap, 3)
