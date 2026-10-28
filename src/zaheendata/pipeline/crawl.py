from __future__ import annotations

import asyncio
import json
import re
from html import unescape
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from zaheendata.fingerprints import detect_tools, summarize_tools
from zaheendata.models import Lead
from zaheendata.normalize import (
    canonicalize_website,
    email_is_role_account,
    is_usable_email,
    looks_like_person,
    normalize_phone,
    parse_socials,
)

USER_AGENT = "ZaheendataLeadResearch/0.1 (+https://github.com/ssashfaque-creator/Zaheendata)"
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.I)
MAX_PAGES = 4
MAX_BYTES = 400_000

HREF_CANDIDATES = (
    "contact",
    "about",
    "services",
    "hvac",
    "plumbing",
    "maintenance",
    "membership",
    "estimate",
    "quote",
    "book",
    "schedule",
)

FOUNDED_RE = re.compile(
    r"(?:since|est\.?|established(?:\s+in)?|founded(?:\s+in)?)\s*(?:in\s*)?((?:19|20)\d{2})",
    re.I,
)

PAIN_PHRASES = (
    "never called back",
    "no callback",
    "didn't call back",
    "hard to reach",
    "billing problem",
    "double billed",
    "invoice wrong",
    "didn't show",
    "no communication",
)


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return unescape(re.sub(r"\s+", " ", soup.get_text(" ", strip=True)))


def _blob(html: str, text: str) -> str:
    return f"{html.lower()}\n{text.lower()}"


def _contains_any(blob: str, needles: Iterable[str]) -> bool:
    return any(needle.lower() in blob for needle in needles)


def _jsonld_blocks(soup: BeautifulSoup) -> list[dict]:
    blocks: list[dict] = []
    for tag in soup.find_all("script"):
        kind = (tag.get("type") or "").lower()
        if "ld+json" not in kind:
            continue
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            blocks.extend(item for item in data["@graph"] if isinstance(item, dict))
        elif isinstance(data, dict):
            blocks.append(data)
    return blocks


def extract_signals(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text = _visible_text(soup)
    blob = _blob(html, text)
    emails = sorted({match.group(0).lower() for match in EMAIL_RE.finditer(html) if not match.group(0).lower().endswith((".png", ".jpg", ".webp", ".svg"))})
    emails = [email for email in emails if is_usable_email(email)]
    summary = summarize_tools(detect_tools(blob))
    booking = summary["booking_provider"]
    store = summary["store_platform"]
    socials = parse_socials(html)
    instagram = socials.get("instagram", "")
    facebook = socials.get("facebook", "")
    youtube = socials.get("youtube", "")
    owner_linkedin = socials.get("owner_linkedin", "")
    owner_on_site = ""
    extra_phones: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        lowered = href.lower()
        if lowered.startswith("tel:"):
            phone = normalize_phone(href.split(":", 1)[-1])
            if phone:
                extra_phones.append(phone)
        if "linkedin.com/in/" in lowered:
            owner_linkedin = owner_linkedin or href.split("?")[0]
            label = anchor.get_text(" ", strip=True)
            if looks_like_person(label):
                owner_on_site = owner_on_site or label
    owner_match = re.search(
        r"(?i:owned by|meet the owner|i['’]m the owner|owner[:\-])\s+([A-Z][a-z]+)\s+([A-Z][a-z]+)\b",
        text,
    )
    if owner_match:
        candidate = f"{owner_match.group(1)} {owner_match.group(2)}"
        if looks_like_person(candidate):
            owner_on_site = owner_on_site or candidate

    for block in _jsonld_blocks(soup):
        email = block.get("email")
        if isinstance(email, str) and is_usable_email(email):
            emails.append(email.lower())
        phone = block.get("telephone")
        if isinstance(phone, str):
            parsed = normalize_phone(phone)
            if parsed:
                extra_phones.append(parsed)
        same = block.get("sameAs")
        if isinstance(same, str):
            same = [same]
        if isinstance(same, list):
            socials.update(parse_socials(" ".join(str(item) for item in same)))

    founded_year = ""
    founded_match = FOUNDED_RE.search(text)
    if founded_match:
        founded_year = founded_match.group(1)
    else:
        for block in _jsonld_blocks(soup):
            founded = str(block.get("foundingDate") or "")
            year_match = re.search(r"((?:19|20)\d{2})", founded)
            if year_match:
                founded_year = year_match.group(1)
                break

    finance_names = [name for name in summary["tech_stack"].split(",") if name in {"greensky", "synchrony", "wisetack", "affirm"}]
    if not finance_names and _contains_any(blob, ("financing available", "0% financing", "monthly payments")):
        finance_names = ["financing_mentioned"]

    has_form = bool(soup.find("form"))
    quote_form = has_form and _contains_any(blob, ("quote", "estimate", "request service", "schedule"))
    pain = [phrase for phrase in PAIN_PHRASES if phrase in blob]

    child_links = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(url, anchor["href"])
        if urlparse(href).netloc != urlparse(url).netloc:
            continue
        path = urlparse(href).path.lower()
        if any(token in path for token in HREF_CANDIDATES):
            child_links.append(canonicalize_website(href) or href)

    return {
        "emails": sorted(set(emails))[:8],
        "booking_provider": booking,
        "online_booking": bool(booking) or "book now" in blob or "schedule online" in blob,
        "quote_form": quote_form,
        "says_call_for_quote": _contains_any(blob, ("call for a quote", "call for quote", "call for pricing", "call for estimate")),
        "emergency_service": _contains_any(blob, ("24/7", "24-7", "24 hour", "emergency", "after hours")),
        "maintenance_plan": _contains_any(blob, ("maintenance plan", "membership", "service agreement", "comfort club", "tune-up plan", "tune up")),
        "recurring_service": _contains_any(blob, ("monthly service", "quarterly", "weekly service", "maintenance contract")),
        "free_estimate": "free estimate" in blob or "free quote" in blob,
        "online_store": bool(store) or "add to cart" in blob,
        "store_platform": store,
        "sells_gift_cards": "gift card" in blob or "gift certificate" in blob,
        "hosts_events": _contains_any(blob, ("tournament", "game night", "workshop", "wedding")),
        "instagram": instagram or socials.get("instagram", ""),
        "facebook": facebook or socials.get("facebook", ""),
        "youtube": youtube or socials.get("youtube", ""),
        "owner_linkedin": owner_linkedin,
        "tech_stack": summary["tech_stack"],
        "tool_categories": summary["tool_categories"],
        "pain_examples": "; ".join(pain),
        "pain_mentions": str(len(pain)),
        "owner_on_site": owner_on_site,
        "founded_year": founded_year,
        "family_owned": _contains_any(blob, ("family owned", "family-owned", "family operated")),
        "financing": ",".join(finance_names),
        "licensed_bonded_insured": _contains_any(
            blob, ("licensed and insured", "licensed, bonded", "bonded and insured", "bonded & insured")
        ),
        "extra_phones": ",".join(dict.fromkeys(extra_phones)),
        "child_links": list(dict.fromkeys(child_links))[:6],
        "text_excerpt": text[:1500],
        "html": html[:MAX_BYTES],
    }


def merge_signals(target: dict, incoming: dict) -> dict:
    for key, value in incoming.items():
        if key in {"emails", "child_links"}:
            target[key] = list(dict.fromkeys([*target.get(key, []), *value]))
        elif key in {"tech_stack", "tool_categories", "extra_phones", "financing"}:
            items = {part for part in f"{target.get(key, '')},{value}".split(",") if part}
            target[key] = ",".join(sorted(items))
        elif key == "pain_examples":
            parts = [part for part in f"{target.get(key, '')}; {value}".split(";") if part.strip()]
            target[key] = "; ".join(dict.fromkeys(parts))
        elif key == "html":
            target[key] = (target.get(key) or "") + "\n" + (value or "")
        elif isinstance(value, bool):
            target[key] = bool(target.get(key)) or value
        elif value and not target.get(key):
            target[key] = value
    return target


def apply_signals(lead: Lead, signals: dict) -> Lead:
    emails = signals.get("emails") or []
    if emails and not lead.owner_email:
        people = [email for email in emails if not email_is_role_account(email)]
        lead.owner_email = (people or emails)[0]
        lead.email_is_role_account = email_is_role_account(lead.owner_email)
    lead.website_crawled = True
    lead.online_booking = signals.get("online_booking") or False
    lead.booking_provider = signals.get("booking_provider") or ""
    lead.quote_form = signals.get("quote_form") or False
    lead.says_call_for_quote = signals.get("says_call_for_quote") or False
    lead.emergency_service = signals.get("emergency_service") or False
    lead.maintenance_plan = signals.get("maintenance_plan") or False
    lead.recurring_service = signals.get("recurring_service") or False
    lead.free_estimate = signals.get("free_estimate") or False
    lead.online_store = signals.get("online_store") or False
    lead.store_platform = signals.get("store_platform") or ""
    lead.sells_gift_cards = signals.get("sells_gift_cards") or False
    lead.hosts_events = signals.get("hosts_events") or False
    lead.instagram = signals.get("instagram") or lead.instagram
    lead.facebook = signals.get("facebook") or lead.facebook
    lead.youtube = signals.get("youtube") or lead.youtube
    lead.owner_linkedin = signals.get("owner_linkedin") or lead.owner_linkedin
    lead.founded_year = signals.get("founded_year") or lead.founded_year
    lead.family_owned = signals.get("family_owned") or lead.family_owned
    lead.financing = signals.get("financing") or lead.financing
    lead.licensed_bonded_insured = signals.get("licensed_bonded_insured") or lead.licensed_bonded_insured
    extra_phones = signals.get("extra_phones") or ""
    lead.extra_phones = extra_phones
    if extra_phones and not lead.owner_phone:
        first = extra_phones.split(",")[0]
        if first != lead.phone:
            lead.owner_phone = first
    lead.tech_stack = signals.get("tech_stack") or lead.tech_stack
    lead.tool_categories = signals.get("tool_categories") or lead.tool_categories
    lead.pain_examples = signals.get("pain_examples") or ""
    lead.pain_mentions = signals.get("pain_mentions") or lead.pain_mentions
    if signals.get("html"):
        lead.extra["_html"] = str(signals["html"])[:MAX_BYTES]
    if signals.get("owner_on_site") and not looks_like_person(lead.owner_name):
        lead.owner_name = signals["owner_on_site"]
        lead.owner_source = "website"
        lead.owner_confidence = 0.7
        lead.owner_title = lead.owner_title or "Owner"
    return lead


async def _fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url, timeout=8.0, follow_redirects=True)
        if response.status_code >= 400:
            return ""
        return response.text[:MAX_BYTES]
    except httpx.HTTPError:
        return ""


async def crawl_lead(client: httpx.AsyncClient, lead: Lead, semaphore: asyncio.Semaphore) -> Lead:
    if not lead.website:
        return lead
    async with semaphore:
        html = await _fetch(client, lead.website)
        if not html:
            lead.website_crawled = False
            return lead
        combined = extract_signals(html, lead.website)
        for child in combined.get("child_links", [])[: MAX_PAGES - 1]:
            child_html = await _fetch(client, child)
            if child_html:
                merge_signals(combined, extract_signals(child_html, child))
        apply_signals(lead, combined)
        return lead


async def crawl_leads(
    leads: list[Lead],
    *,
    concurrency: int = 8,
    on_html: Callable[[Lead, str], None] | None = None,
) -> list[Lead]:
    if not leads:
        return leads
    semaphore = asyncio.Semaphore(concurrency)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"}
    async with httpx.AsyncClient(headers=headers, http2=False) as client:
        await asyncio.gather(*(crawl_lead(client, lead, semaphore) for lead in leads))
    for lead in leads:
        html = lead.extra.pop("_html", "")
        if on_html and html:
            on_html(lead, html)
    return leads
