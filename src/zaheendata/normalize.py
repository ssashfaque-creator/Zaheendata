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

PLACEHOLDER_EMAIL_LOCALS = {"user", "email", "you", "name", "test", "noreply", "no-reply", "username"}
PLACEHOLDER_EMAIL_HOSTS = {"domain.com", "email.com", "yourdomain.com", "example.com", "sentry.io"}


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
    # ALL CAPS 5+ tokens is usually a firm. 2–4 tokens is often a person
    # on license files ("CHARLES RAY WILKS").
    return text.isupper() and len(text.split()) >= 5


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
    "electric",
    "electrical",
    "lighting",
    "installation",
    "contracting",
    "contractor",
    "company",
    "generators",
    "inc",
    "llc",
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
        text = f"{first} {last}".strip()
    if text.isupper():
        text = text.title()
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


def email_is_placeholder(email: str | None) -> bool:
    raw = (email or "").strip().lower()
    if "@" not in raw:
        return True
    local, _, host = raw.partition("@")
    if local in PLACEHOLDER_EMAIL_LOCALS:
        return True
    return host in PLACEHOLDER_EMAIL_HOSTS or host.endswith(".png")


def email_is_role_account(email: str | None) -> bool:
    local = (email or "").split("@", 1)[0].lower()
    return local in ROLE_LOCALS or local.endswith("office")


def is_usable_email(email: str | None) -> bool:
    raw = (email or "").strip()
    if not raw or "@" not in raw or email_is_placeholder(raw):
        return False
    return True


def parse_socials(blob: str | None) -> dict[str, str]:
    found: dict[str, str] = {}
    text = blob or ""
    for match in re.finditer(r"https?://[^\s\"'<>]+", text, re.I):
        url = match.group(0).rstrip(").,]}")
        host = domain_from_url(url)
        if "instagram.com" in host and "instagram" not in found:
            found["instagram"] = url.split("?")[0]
        elif "facebook.com" in host and "facebook" not in found:
            found["facebook"] = url.split("?")[0]
        elif ("youtube.com" in host or "youtu.be" in host) and "youtube" not in found:
            found["youtube"] = url.split("?")[0]
        elif "linkedin.com/in/" in url.lower() and "owner_linkedin" not in found:
            found["owner_linkedin"] = url.split("?")[0]
        elif "linkedin.com/company/" in url.lower() and "linkedin" not in found:
            found["linkedin"] = url.split("?")[0]
    return found


def token_set(value: str | None) -> set[str]:
    return {token for token in normalize_name(value).split() if len(token) > 1}


TRADE_NAME_TOKENS = {
    "ac",
    "air",
    "austin",
    "and",
    "company",
    "conditioning",
    "comfort",
    "heating",
    "htg",
    "hvac",
    "inc",
    "llc",
    "mechanical",
    "of",
    "plumbing",
    "plumber",
    "service",
    "services",
    "texas",
    "the",
}


def distinctive_name_key(value: str | None) -> str:
    tokens = [
        token
        for token in normalize_name(value).split()
        if token not in TRADE_NAME_TOKENS and len(token) > 2
    ]
    return "".join(tokens)


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
