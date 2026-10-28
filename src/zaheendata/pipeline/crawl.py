from __future__ import annotations

import asyncio
import re
from html import unescape
from typing import Callable, Iterable
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from zaheendata.fingerprints import detect_tools, summarize_tools
from zaheendata.models import Lead
from zaheendata.normalize import canonicalize_website, email_is_role_account, looks_like_person

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


def extract_signals(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    text = _visible_text(soup)
    blob = _blob(html, text)
    emails = sorted({match.group(0).lower() for match in EMAIL_RE.finditer(html) if not match.group(0).lower().endswith((".png", ".jpg", ".webp", ".svg"))})
    emails = [email for email in emails if "example.com" not in email and "sentry" not in email]
    summary = summarize_tools(detect_tools(blob))
    booking = summary["booking_provider"]
    store = summary["store_platform"]
    instagram = ""
    owner_on_site = ""
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        if "instagram.com" in href and not instagram:
            instagram = href.split("?")[0]
        lowered = href.lower()
        if "linkedin.com/in/" in lowered and looks_like_person(anchor.get_text(" ", strip=True)):
            owner_on_site = owner_on_site or anchor.get_text(" ", strip=True)
    owner_match = re.search(
        r"(?i:owned by|meet the owner|i['’]m|owner)\s*[:\-]?\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})",
        text,
    )
    if owner_match and looks_like_person(owner_match.group(1)):
        owner_on_site = owner_on_site or owner_match.group(1)

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
        "emails": emails[:8],
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
        "instagram": instagram,
        "tech_stack": summary["tech_stack"],
        "tool_categories": summary["tool_categories"],
        "pain_examples": "; ".join(pain),
        "owner_on_site": owner_on_site,
        "child_links": list(dict.fromkeys(child_links))[:6],
        "text_excerpt": text[:1500],
        "html": html[:MAX_BYTES],
    }


def merge_signals(target: dict, incoming: dict) -> dict:
    for key, value in incoming.items():
        if key in {"emails", "child_links"}:
            target[key] = list(dict.fromkeys([*target.get(key, []), *value]))
        elif key in {"tech_stack", "tool_categories"}:
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
    lead.tech_stack = signals.get("tech_stack") or ""
    lead.tool_categories = signals.get("tool_categories") or ""
    lead.pain_examples = signals.get("pain_examples") or ""
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
