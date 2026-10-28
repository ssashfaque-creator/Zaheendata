"""Website crawler + feature extraction.

Crawls the homepage and a handful of high-signal internal pages (contact, about, services, book,
shop, events, team ...) and turns the content into the boolean ``WebsiteFeatures`` the scoring
rules consume: online booking vs "call for a quote", maintenance plans, emergency service, retail
products, gift cards, events, Instagram selling, owner name candidates, emails, phones, and so on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import HttpClient, RobotsDisallowed
from ..logging import get_logger
from ..models import WebsiteFeatures
from ..normalize import extract_domain
from ..settings import Settings, get_settings

log = get_logger(__name__)


@dataclass
class CrawledPage:
    url: str
    status: int
    html: str
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    script_urls: list[str] = field(default_factory=list)
    link_urls: list[str] = field(default_factory=list)
    iframe_urls: list[str] = field(default_factory=list)
    forms: list[str] = field(default_factory=list)  # text content of each form
    anchors: list[tuple[str, str]] = field(default_factory=list)  # (href, anchor text)
    jsonld: list[dict] = field(default_factory=list)


FOLLOW_HINTS = (
    "contact",
    "about",
    "service",
    "book",
    "schedule",
    "appointment",
    "shop",
    "store",
    "product",
    "event",
    "tournament",
    "calendar",
    "team",
    "staff",
    "gallery",
    "portfolio",
    "project",
    "pricing",
    "plan",
    "membership",
    "quote",
    "estimate",
    "financing",
    "commercial",
    "residential",
    "location",
    "gift",
    "wedding",
    "groom",
    "uniform",
    "maintenance",
)

BOOKING_HOSTS = (
    "vagaro.com",
    "fresha.com",
    "booksy.com",
    "squareup.com/appointments",
    "square.site",
    "acuityscheduling.com",
    "squarespacescheduling.com",
    "calendly.com",
    "mindbodyonline.com",
    "schedulicity.com",
    "setmore.com",
    "glossgenius.com",
    "styleseat.com",
    "joinblvd.com",
    "blvd.co",
    "housecallpro.com",
    "getjobber.com",
    "servicetitan.com",
    "servicefusion.com",
    "workiz.com",
    "gingrapp.com",
    "petexec.net",
    "zenoti.com",
    "meevo.com",
    "mytime.com",
    "phorest.com",
    "gettimely.com",
    "salonbiz.com",
    "salonultimate.com",
    "booker.com",
    "zocdoc.com",
    "appointy.com",
    "simplybook.me",
    "youcanbook.me",
    "bookedin.com",
    "picktime.com",
    "schedulista.com",
    "genbook.com",
    "boulevard.io",
    "rosy.com",
    "daysmart.com",
    "dash.booksy",
)
STORE_HOSTS = (
    "myshopify.com",
    "etsy.com/shop",
    "square.site",
    "shop.square",
    "bigcommerce.com",
    "ecwid.com",
    "faire.com",
    "squareup.com/store",
)

P = {
    "says_call_for_quote": r"call (us )?(today |now )?(for|to get) (a |your )?(free )?(quote|estimate|pricing|consultation)|call for (pricing|a quote|an estimate|rates)|please call (us )?(for|to)|contact us for (a |pricing|quote)",
    "free_estimate": r"free (estimate|quote|consultation|inspection)s?|(estimate|quote)s? (are )?free|no.obligation (quote|estimate)",
    "emergency_service": r"24[ /-]?7|24.hours?|emergency (service|repair|plumb|hvac|electric|call|response)|same.day service|after.hours|emergencies",
    "maintenance_plan": r"maintenance (plan|agreement|program|membership|contract)s?|service (plan|agreement|membership|contract)s?|membership (plan|program)|comfort club|preventive maintenance|preventative maintenance|annual (plan|contract|service agreement)|protection plan|tune.up (plan|program)|quarterly (plan|service|treatment)s?|(monthly|quarterly|annual|bi.monthly) (service|treatment|visit)s? plan|pool (service|cleaning) plan",
    "recurring_service": r"weekly|bi.?weekly|every (other )?week|monthly service|quarterly|recurring|route|year.round|seasonal (contract|program|service)|lawn care program|ongoing (service|care)|regular(ly scheduled)? (service|visit|maintenance)|subscription",
    "financing": r"financing (available|options|plans)|0% (apr|interest)|pay over time|monthly payments|affirm|wisetack|greensky|synchrony",
    "insurance_storm_work": r"insurance (claim|company|adjuster|work|approved)|storm (damage|restoration|repair)|hail (damage|repair)|wind damage|we work with (your )?insurance",
    "inspection_request": r"(free|schedule|request|book) (a |an |your )?(roof |home |pest |termite )?inspection|inspection request",
    "residential_focus": r"residential|homeowners?|your home|home services|houses|family home|homes in",
    "commercial_focus": r"commercial|industrial|office buildings?|property manag|new construction|general contractors|tenant improvement|bid",
    "project_gallery": r"gallery|portfolio|our (work|projects|recent work)|before (and|&) after|recent projects|project photos|see our work|featured projects",
    "sells_gift_cards": r"gift ?cards?|gift certificates?|e-?gift",
    "walk_ins_welcome": r"walk-?ins?( are)? (welcome|accepted)|no appointment (necessary|needed|required)|walk-?ins? only|walk right in",
    "retail_products": r"(we )?(carry|sell|retail|offer|stock) [^.]{0,40}(products|product lines?|brands)|shop (our )?products|retail (products|boutique|area|shelves|store)|professional products|redken|olaplex|k[ée]rastase|aveda|paul mitchell|moroccanoil|pureology|davines|oribe|opi|cnd|essie|dnd|merchandise|products? for (sale|purchase)",
    "hosts_events": r"events?|tournaments?|game nights?|leagues?|friday night magic|commander night|prerelease|workshops?|classes|open play|demo day|release party|meetups?|story ?time|in-store events",
    "wedding_event_work": r"weddings?|bridal|event (floral|flowers|design|work)|corporate events?|funeral|sympathy|events? (floral|flowers)|special events",
    "team_uniform_sales": r"team (sales|orders|uniforms|outfitting|dealer|pricing)|uniforms?|custom (jerseys|apparel|screen printing|embroidery)|bulk (orders?|pricing)|school (teams|orders|spirit)|team store|league (orders|pricing)",
    "grooming_services": r"grooming|groomers?|self.serve (dog |pet )?wash|nail trims?|bath (and|&) brush|de-?shedding|spa (services|packages) for (dogs|pets)",
    "multiple_crews_or_staff": r"\b(\d{1,3}|two|three|four|five|six|seven|eight|nine|ten|several|multiple|dozens of) (crews|teams|trucks|vans|technicians|techs|stylists|employees|staff members|team members|installers|cleaners|painters|electricians|plumbers|groomers)\b|our (crews|teams|technicians|stylists|team members)|meet (the|our) (team|crew|staff|stylists)|team of (\d+|experienced|professional|licensed)",
    "commercial_clients": r"commercial (clients|accounts|cleaning|customers|properties|facilities|janitorial)|office (cleaning|buildings)|medical (office|facilit)|our clients include|trusted by|clients we serve|facilities we (clean|serve)|day porter|nightly cleaning|janitorial",
    "franchise_language": r"franchis(e|ee|ing)|find a location near you|our locations|locations near you|locally owned and operated franchise|each location is independently",
    "locally_owned_language": r"family.owned|locally owned|owner.operated|woman.owned|veteran.owned|since (19|20)\d{2}|founded in (19|20)\d{2}|independently owned",
    "instagram_shop_signals": r"shop (our )?(instagram|insta|ig)|live ?sales?|live shopping|facebook live|comment ?sold|shop the (feed|gram|look)|instagram shop|link in bio|shop (on|via|through) instagram|go live|(our )?lives? (every|on) (monday|tuesday|wednesday|thursday|friday|saturday|sunday)",
    "online_store_text": r"shop online|online store|buy online|add to cart|shop now|our (online )?shop|order online|shop (the )?(collection|new arrivals)|view cart|checkout",
    "booking_text": r"book (online|now|an appointment|your appointment|appointment online)|schedule (online|now|an appointment|service online|your service)|online (booking|scheduling)|request (an appointment|service) online|reserve (your|a) (spot|appointment)",
    "quote_link_text": r"(get|request|free) (a |an |your )?(free )?(quote|estimate|proposal|consultation)|quote request|estimate request",
}
PATTERNS = {k: re.compile(v, re.IGNORECASE) for k, v in P.items()}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?:\+1[ .-]?)?\(?\b\d{3}\)?[ .-]\d{3}[ .-]\d{4}\b")
CHAIRS_RE = re.compile(r"\b(\d{1,2})\s*(?:styling\s+)?(chairs?|stations|stylists|suites)\b", re.IGNORECASE)
LOCATIONS_RE = re.compile(r"\b(\d{1,3})\s+(?:convenient\s+)?locations\b", re.IGNORECASE)
_NAME = r"[A-Z][a-zA-Z'’-]+(?:\s+(?:[A-Z]\.\s+)?[A-Z][a-zA-Z'’-]+){1,2}"
_TITLE = r"(?i:owner|founder|co-?owner|co-?founder|proprietor|president|master stylist and owner)"
OWNER_RE = [
    re.compile(rf"\b({_TITLE})\b[\s:,\-–]+({_NAME})"),
    re.compile(rf"\b({_NAME})[\s,\-–]+(?:(?i:is\s+(?:the|our)\s+))?({_TITLE})\b"),
    re.compile(rf"\b(?i:owned|founded|started)\s+(?:(?i:and operated)\s+)?(?i:by)\s+({_NAME})"),
]
SOCIAL_HOSTS = {
    "instagram.com": "instagram",
    "facebook.com": "facebook",
    "tiktok.com": "tiktok",
    "linkedin.com": "linkedin",
    "youtube.com": "youtube",
    "yelp.com": "yelp",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "pinterest.com": "pinterest",
    "nextdoor.com": "nextdoor",
    "houzz.com": "houzz",
    "thumbtack.com": "thumbtack",
    "angi.com": "angi",
}
GENERIC_NAME_WORDS = {
    "Our",
    "The",
    "Team",
    "Contact",
    "About",
    "Us",
    "Meet",
    "Call",
    "Today",
    "Now",
    "Service",
    "Services",
    "Owner",
    "Family",
    "Operated",
    "Locally",
    "Since",
    "Book",
    "Online",
    "Read",
    "More",
    "Learn",
    "Home",
    "Shop",
    "Menu",
    "Instagram",
    "Facebook",
    "Twitter",
    "TikTok",
    "LinkedIn",
    "Email",
    "Phone",
    "Gift",
    "Cards",
    "Reviews",
    "Blog",
    "Gallery",
    "Pricing",
    "Hours",
    "Location",
    "Locations",
    "Staff",
    "Stylists",
    "Master",
    "Stylist",
    "Licensed",
    "Insured",
    "Plumbing",
    "Electric",
    "Salon",
    "Inc",
    "LLC",
}


def _text_of(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ", strip=True).split())


def parse_page(url: str, status: int, html: str, headers: dict[str, str]) -> CrawledPage:
    soup = BeautifulSoup(html, "lxml")
    jsonld = []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
            jsonld.extend(data if isinstance(data, list) else [data])
        except (json.JSONDecodeError, TypeError):
            continue
    scripts = [urljoin(url, str(s.get("src"))) for s in soup.find_all("script", src=True)]
    iframes = [urljoin(url, str(f.get("src"))) for f in soup.find_all("iframe", src=True)]
    anchors = [
        (urljoin(url, str(a.get("href"))), a.get_text(" ", strip=True)) for a in soup.find_all("a", href=True)
    ]
    links = [href for href, _ in anchors] + [
        urljoin(url, str(lk.get("href"))) for lk in soup.find_all("link", href=True)
    ]
    forms = [
        " ".join(f.get_text(" ", strip=True).split())
        + " "
        + " ".join(
            str(i.get("name") or i.get("placeholder") or i.get("id") or "")
            for i in f.find_all(["input", "textarea", "select"])
        )
        for f in soup.find_all("form")
    ]
    return CrawledPage(
        url=url,
        status=status,
        html=html,
        text=_text_of(soup),
        headers=headers,
        script_urls=scripts,
        link_urls=links,
        iframe_urls=iframes,
        forms=forms,
        anchors=anchors,
        jsonld=jsonld,
    )


def _candidate_links(page: CrawledPage, domain: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for href, text in page.anchors:
        parts = urlsplit(href)
        host = parts.netloc.lower().split(":")[0]
        host = host[4:] if host.startswith("www.") else host
        if host != domain or parts.scheme not in ("http", "https"):
            continue
        clean = parts._replace(fragment="", query="").geturl().rstrip("/")
        hay = (parts.path + " " + text).lower()
        if clean in seen or not any(h in hay for h in FOLLOW_HINTS):
            continue
        if re.search(r"\.(pdf|jpg|jpeg|png|gif|zip|mp4)$", parts.path, re.IGNORECASE):
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _clean_name(name: str) -> str | None:
    tokens = [t.strip(".,") for t in name.split()]
    tokens = [t for t in tokens if t and t.lower() != "and"]
    # strip generic / navigation words that the greedy capture picked up at either end
    while tokens and tokens[-1] in GENERIC_NAME_WORDS:
        tokens.pop()
    while tokens and tokens[0] in GENERIC_NAME_WORDS:
        tokens.pop(0)
    if any(t in GENERIC_NAME_WORDS for t in tokens):
        return None
    if 2 <= len(tokens) <= 3 and all(t[0].isupper() for t in tokens):
        return " ".join(tokens)
    return None


def extract_features(pages: list[CrawledPage], domain: str | None) -> WebsiteFeatures:
    f = WebsiteFeatures(crawled=bool(pages), pages_crawled=len(pages))
    if not pages:
        return f
    f.final_url = pages[0].url
    text = " \n".join(p.text for p in pages)
    all_links = [u for p in pages for u in p.link_urls + p.iframe_urls + p.script_urls]
    anchor_text = " ".join(t for p in pages for _, t in p.anchors)
    lower_links = [u.lower() for u in all_links]

    flags = {k: bool(PATTERNS[k].search(text)) for k in PATTERNS}
    for key in (
        "says_call_for_quote",
        "free_estimate",
        "emergency_service",
        "maintenance_plan",
        "recurring_service",
        "financing",
        "insurance_storm_work",
        "inspection_request",
        "residential_focus",
        "commercial_focus",
        "project_gallery",
        "sells_gift_cards",
        "walk_ins_welcome",
        "retail_products",
        "hosts_events",
        "wedding_event_work",
        "team_uniform_sales",
        "grooming_services",
        "multiple_crews_or_staff",
        "commercial_clients",
        "franchise_language",
        "locally_owned_language",
        "instagram_shop_signals",
    ):
        setattr(f, key, flags[key])

    # booking
    provider = next((h for h in BOOKING_HOSTS for u in lower_links if h in u), None)
    f.booking_provider = provider.split("/")[0] if provider else None
    f.has_online_booking = bool(provider) or bool(PATTERNS["booking_text"].search(anchor_text))

    # forms
    for p in pages:
        for form in p.forms:
            fl = form.lower()
            if any(k in fl for k in ("quote", "estimate", "proposal", "request service", "project")):
                f.has_quote_form = True
            if any(k in fl for k in ("message", "email", "comments", "textarea", "your name", "phone")):
                f.has_contact_form = True
    if PATTERNS["quote_link_text"].search(anchor_text) and any(
        re.search(r"(quote|estimate)", u) for u in lower_links if domain and domain in u
    ):
        f.has_quote_form = True
    # "call for a quote" is only a pain signal when there is no online alternative
    if f.says_call_for_quote and (f.has_online_booking or f.has_quote_form):
        f.says_call_for_quote = False

    # online store
    store_host = next((h for h in STORE_HOSTS for u in lower_links if h in u), None)
    has_shop_path = any(
        re.search(r"/(shop|store|products|collections|cart|checkout)(/|$)", urlsplit(u).path)
        for u in lower_links
        if domain and domain in u
    )
    f.online_store = (
        bool(store_host) or has_shop_path or bool(PATTERNS["online_store_text"].search(anchor_text))
    )
    if store_host:
        f.store_platform = {
            "myshopify.com": "shopify",
            "etsy.com/shop": "etsy",
            "square.site": "square-online",
            "shop.square": "square-online",
            "squareup.com/store": "square-online",
            "bigcommerce.com": "bigcommerce",
            "ecwid.com": "ecwid",
            "faire.com": "faire",
        }.get(store_host)
    html_blob = "\n".join(p.html for p in pages).lower()
    if not f.store_platform:
        if "cdn.shopify.com" in html_blob or "shopify" in html_blob:
            f.store_platform = "shopify"
        elif "woocommerce" in html_blob:
            f.store_platform = "woocommerce"
        elif "etsy.com/shop" in html_blob:
            f.store_platform = "etsy"
    if f.store_platform and not f.online_store:
        f.online_store = True

    # hints
    chairs = [int(m.group(1)) for m in CHAIRS_RE.finditer(text) if 0 < int(m.group(1)) < 60]
    f.chair_count_hint = max(chairs) if chairs else None
    locs = [int(m.group(1)) for m in LOCATIONS_RE.finditer(text) if 1 < int(m.group(1)) < 500]
    f.location_count_hint = max(locs) if locs else None

    # owner names
    names: list[str] = []
    for p in pages:
        for item in p.jsonld:
            for key in ("founder", "employee", "author"):
                val = item.get(key) if isinstance(item, dict) else None
                if isinstance(val, dict) and val.get("name"):
                    names.append(str(val["name"]))
                elif isinstance(val, list):
                    names.extend(str(v.get("name")) for v in val if isinstance(v, dict) and v.get("name"))
    for rx in OWNER_RE:
        for m in rx.finditer(text):
            groups = [g for g in m.groups() if g]
            for g in groups:
                if g and g[0].isupper() and g.lower() not in ("owner", "founder", "president", "proprietor"):
                    names.append(g)
    cleaned = []
    for n in names:
        c = _clean_name(n)
        if c and c not in cleaned:
            cleaned.append(c)
    f.owner_name_candidates = cleaned[:5]

    # contacts
    emails = sorted(
        {
            e.lower()
            for e in EMAIL_RE.findall(text + " " + " ".join(all_links))
            if not re.search(r"\.(png|jpg|gif|svg|webp)$", e, re.IGNORECASE)
            and "example." not in e
            and "sentry" not in e
            and "wixpress" not in e
        }
    )
    f.emails = [e for e in emails if not domain or domain in e] + [
        e for e in emails if domain and domain not in e
    ][:3]
    f.phones = sorted({re.sub(r"\D", "", ph)[-10:] for ph in PHONE_RE.findall(text)})[:5]
    for p in pages:
        for item in p.jsonld:
            if not isinstance(item, dict):
                continue
            addr = item.get("address")
            if isinstance(addr, list):
                addr = addr[0] if addr else None
            if isinstance(addr, dict):
                f.street_address = f.street_address or addr.get("streetAddress")
                f.address_city = f.address_city or addr.get("addressLocality")
                f.address_region = f.address_region or addr.get("addressRegion")
                f.address_postal = f.address_postal or addr.get("postalCode")
            tel = item.get("telephone")
            if isinstance(tel, str):
                digits = re.sub(r"\D", "", tel)[-10:]
                if len(digits) == 10 and digits not in f.phones:
                    f.phones.insert(0, digits)
    for u in all_links:
        host = urlsplit(u).netloc.lower()
        host = host[4:] if host.startswith("www.") else host
        for sh, label in SOCIAL_HOSTS.items():
            if (host == sh or host.endswith("." + sh)) and label not in f.social_links:
                f.social_links[label] = u
    return f


class WebsiteCrawler:
    def __init__(self, http: HttpClient, settings: Settings | None = None):
        self.http = http
        self.settings = settings or get_settings()

    async def crawl(self, website: str) -> list[CrawledPage]:
        domain = extract_domain(website)
        if not domain:
            return []
        pages: list[CrawledPage] = []
        queue = [website]
        visited: set[str] = set()
        while queue and len(pages) < self.settings.website_max_pages:
            url = queue.pop(0)
            key = url.rstrip("/").lower()
            if key in visited:
                continue
            visited.add(key)
            try:
                resp = await self.http.get(
                    url,
                    check_robots=len(pages) > 0,
                    max_bytes=self.settings.website_max_bytes,
                    headers=None
                    if len(pages)
                    else {
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                        )
                    },
                )
            except RobotsDisallowed:
                log.info("website.robots_disallowed", url=url)
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("website.fetch_failed", url=url, error=str(exc))
                if not pages:
                    # retry https if we started with http (or vice versa)
                    alt = url.replace("http://", "https://", 1) if url.startswith("http://") else None
                    if alt and alt.rstrip("/").lower() not in visited:
                        queue.insert(0, alt)
                continue
            ctype = resp.headers.get("content-type", "")
            if resp.status_code >= 400 or ("html" not in ctype and "xml" not in ctype and ctype):
                continue
            page = parse_page(str(resp.url), resp.status_code, resp.text, dict(resp.headers))
            pages.append(page)
            if len(pages) == 1:
                final_domain = extract_domain(str(resp.url)) or domain
                domain = final_domain
                queue.extend(_candidate_links(page, domain))
        return pages
