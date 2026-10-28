"""Free discovery of website, address and phone before Google Places runs.

Used in the enrich stage so a registry-only long list (often just a legal name) can still be
crawled and scored. Sources, in order: Nominatim (OSM), SAM.gov, Yelp, DuckDuckGo HTML search.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from ..http import HttpClient
from ..logging import get_logger
from ..models import Address, Business, GeoPoint, OwnerContact
from ..normalize import (
    extract_domain,
    is_social_or_directory_url,
    normalize_name,
    normalize_phone,
    normalize_url,
)
from ..settings import Settings, get_settings
from .website import WebsiteFeatures

log = get_logger(__name__)

DDG_URL = "https://html.duckduckgo.com/html/"
PHOTON_URL = "https://photon.komoot.io/api/"
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
WEBSITE_MATCH_THRESHOLD = 55
NOMINATIM_MATCH_THRESHOLD = 80
PHOTON_MATCH_THRESHOLD = 85
# DDG's HTML endpoint returns an empty homepage (HTTP 202) for bot UAs.
DDG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
    "Referer": "https://html.duckduckgo.com/",
}

JUNK_HOSTS = {
    "facebook.com",
    "instagram.com",
    "yelp.com",
    "linkedin.com",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "youtube.com",
    "google.com",
    "bing.com",
    "duckduckgo.com",
    "apple.com",
    "bbb.org",
    "yellowpages.com",
    "whitepages.com",
    "mapquest.com",
    "angi.com",
    "houzz.com",
    "thumbtack.com",
    "nextdoor.com",
    "manta.com",
    "dnb.com",
    "zoominfo.com",
    "buzzfile.com",
    "chamberofcommerce.com",
    "wikipedia.org",
    "crunchbase.com",
    "bloomberg.com",
    "opencorporates.com",
    "hvacservice.io",
    "ncwakehvac.com",
    "cybo.com",
    "bizapedia.com",
    "bizarchive.com",
    "buildzoom.com",
    "thebluebook.com",
    "home8.org",
    "southernhomeservices.com",
    "georgetownhomeservices.com",
    "zabalist.com",
    "hotfrog.com",
    "brownbook.net",
    "cylex.us.com",
}


def _href_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def unwrap_ddg_href(href: str) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    qs = parse_qs(parsed.query)
    if "uddg" in qs:
        href = unquote(qs["uddg"][0])
    return normalize_url(href)


def parse_ddg_html(html: str) -> list[str]:
    """Pull result URLs out of DuckDuckGo's HTML search page (several layouts)."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.select("a.result__a, a.result-link, a.result__url"):
        url = unwrap_ddg_href(_href_str(a.get("href")))
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    if not urls:
        for a in soup.find_all("a", href=True):
            url = unwrap_ddg_href(_href_str(a.get("href")))
            if url and url not in seen and not is_social_or_directory_url(url):
                host = extract_domain(url) or ""
                if host.endswith("duckduckgo.com") or host.endswith("bing.com"):
                    continue
                seen.add(url)
                urls.append(url)
    return urls


def pick_website(business_name: str, urls: list[str]) -> str | None:
    """Choose the result whose domain looks most like the business name; skip directories."""
    target = normalize_name(business_name)
    if not target or not urls:
        return None
    ranked: list[tuple[float, str]] = []
    for url in urls:
        host = extract_domain(url)
        if not host or is_social_or_directory_url(url):
            continue
        if any(host == junk or host.endswith("." + junk) for junk in JUNK_HOSTS):
            continue
        stem = host.split(".")[0].replace("-", " ").replace("_", " ")
        score = float(fuzz.token_set_ratio(target, stem))
        ranked.append((score, url))
    ranked.sort(key=lambda pair: -pair[0])
    if ranked and ranked[0][0] >= WEBSITE_MATCH_THRESHOLD:
        return ranked[0][1]
    return None


_STATE_NAMES = {
    "AL": "ALABAMA",
    "AK": "ALASKA",
    "AZ": "ARIZONA",
    "AR": "ARKANSAS",
    "CA": "CALIFORNIA",
    "CO": "COLORADO",
    "CT": "CONNECTICUT",
    "DC": "DISTRICT OF COLUMBIA",
    "DE": "DELAWARE",
    "FL": "FLORIDA",
    "GA": "GEORGIA",
    "HI": "HAWAII",
    "IA": "IOWA",
    "ID": "IDAHO",
    "IL": "ILLINOIS",
    "IN": "INDIANA",
    "KS": "KANSAS",
    "KY": "KENTUCKY",
    "LA": "LOUISIANA",
    "MA": "MASSACHUSETTS",
    "MD": "MARYLAND",
    "ME": "MAINE",
    "MI": "MICHIGAN",
    "MN": "MINNESOTA",
    "MO": "MISSOURI",
    "MS": "MISSISSIPPI",
    "MT": "MONTANA",
    "NC": "NORTH CAROLINA",
    "ND": "NORTH DAKOTA",
    "NE": "NEBRASKA",
    "NH": "NEW HAMPSHIRE",
    "NJ": "NEW JERSEY",
    "NM": "NEW MEXICO",
    "NV": "NEVADA",
    "NY": "NEW YORK",
    "OH": "OHIO",
    "OK": "OKLAHOMA",
    "OR": "OREGON",
    "PA": "PENNSYLVANIA",
    "RI": "RHODE ISLAND",
    "SC": "SOUTH CAROLINA",
    "SD": "SOUTH DAKOTA",
    "TN": "TENNESSEE",
    "TX": "TEXAS",
    "UT": "UTAH",
    "VA": "VIRGINIA",
    "VT": "VERMONT",
    "WA": "WASHINGTON",
    "WI": "WISCONSIN",
    "WV": "WEST VIRGINIA",
    "WY": "WYOMING",
}


def _state_matches(want: str, got: str) -> bool:
    w, g = want.strip().upper(), got.strip().upper()
    if w == g:
        return True
    return _STATE_NAMES.get(w) == g or _STATE_NAMES.get(g) == w


def census_point(payload: dict[str, Any]) -> GeoPoint | None:
    matches = ((payload.get("result") or {}).get("addressMatches")) or []
    if not matches:
        return None
    coords = matches[0].get("coordinates") or {}
    if coords.get("y") is None or coords.get("x") is None:
        return None
    return GeoPoint(lat=float(coords["y"]), lng=float(coords["x"]))


def photon_pick(
    business_name: str, city: str | None, region: str | None, features: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Choose an OSM/Photon hit only when the name (and city, if we have one) actually match."""
    target = normalize_name(business_name)
    if not target or not features:
        return None
    want_city = normalize_name(city) if city else ""
    want_region = (region or "").strip().upper()
    best: tuple[float, dict[str, Any]] | None = None
    for feat in features:
        props = feat.get("properties") or {}
        name = str(props.get("name") or "")
        score = float(fuzz.token_set_ratio(target, normalize_name(name)))
        if score < PHOTON_MATCH_THRESHOLD:
            continue
        got_city = normalize_name(str(props.get("city") or props.get("locality") or ""))
        if want_city and got_city and float(fuzz.token_set_ratio(want_city, got_city)) < 80:
            continue
        state = str(props.get("state") or "")
        if want_region and state and not _state_matches(want_region, state):
            continue
        if best is None or score > best[0]:
            best = (score, feat)
    return best[1] if best else None


def harvest_from_website(b: Business) -> None:
    """Copy phone, email, owner and street address discovered on the crawled site onto the record."""
    wf: WebsiteFeatures = b.website_features
    if not wf.crawled:
        return
    if not b.phone_e164 and wf.phones:
        b.phone_e164 = normalize_phone(wf.phones[0])
    if not b.owner.name and wf.owner_name_candidates:
        b.owner = OwnerContact(
            name=wf.owner_name_candidates[0],
            email=b.owner.email,
            phone=b.owner.phone,
            source="website",
            confidence=0.45,
        )
    if not b.owner.email and wf.emails:
        b.owner.email = wf.emails[0]
        b.owner.source = b.owner.source or "website"
        b.owner.confidence = max(b.owner.confidence or 0, 0.55)
    addr = b.address or Address()
    changed = False
    if not addr.line1 and wf.street_address:
        addr.line1 = wf.street_address
        changed = True
    if not addr.city and wf.address_city:
        addr.city = wf.address_city
        changed = True
    if not addr.region and wf.address_region:
        addr.region = wf.address_region
        changed = True
    if not addr.postal_code and wf.address_postal:
        addr.postal_code = wf.address_postal
        changed = True
    if changed:
        b.address = addr


class DirectoryDiscoverer:
    """Fill missing website / address / phone from free public sources."""

    def __init__(self, http: HttpClient, settings: Settings | None = None):
        self.http = http
        self.settings = settings or get_settings()
        self.fallback_where = ""
        self.http.set_host_rate("html.duckduckgo.com", 0.5, concurrency=1)
        self.http.set_host_rate("photon.komoot.io", 1.0, concurrency=1)
        self.http.set_host_rate("geocoding.geo.census.gov", 1.0, concurrency=1)
        host = urlparse(self.settings.nominatim_url).netloc
        if host:
            self.http.set_host_rate(host, 0.8, concurrency=1)

    def _where(self, b: Business) -> str:
        addr = b.address
        city = (addr.city if addr else None) or ""
        region = (addr.region if addr else None) or ""
        built = ", ".join(p for p in (city, region) if p)
        return built or self.fallback_where

    async def fill(self, b: Business) -> dict[str, str]:
        """Best-effort fill. Returns a map of which fields came from which source."""
        found: dict[str, str] = {}
        addr = b.address
        if addr and addr.line1 and b.location is None:
            try:
                if await self._census(b):
                    found["geo"] = "census"
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.census_failed", name=b.name, error=str(exc)[:200])
        try:
            if await self._photon(b):
                found.setdefault("poi", "photon")
        except Exception as exc:  # noqa: BLE001
            log.debug("discover.photon_failed", name=b.name, error=str(exc)[:200])
        if addr and addr.line1:
            try:
                if await self._nominatim(b):
                    found.setdefault("geo", "nominatim")
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.nominatim_failed", name=b.name, error=str(exc)[:200])
        if not b.website:
            try:
                url = await self._duckduckgo(b)
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.ddg_failed", name=b.name, error=str(exc)[:200])
                url = None
            if url:
                b.website = url
                b.domain = extract_domain(url)
                b.attributes["website_source"] = "duckduckgo"
                found["website"] = "duckduckgo"
        if found:
            log.info("discover.filled", name=b.name, **found)
        return found

    async def _census(self, b: Business) -> bool:
        addr = b.address
        if not addr or not addr.line1:
            return False
        data = await self.http.get_json(
            CENSUS_GEOCODER_URL,
            params={
                "street": addr.line1,
                "city": addr.city or "",
                "state": addr.region or "",
                "zip": addr.postal_code or "",
                "benchmark": "Public_AR_Current",
                "format": "json",
            },
        )
        point = census_point(data if isinstance(data, dict) else {})
        if point is None:
            return False
        b.location = point
        b.attributes["geo_source"] = "census"
        return True

    async def _photon(self, b: Business) -> bool:
        where = self._where(b)
        query = f"{b.name} {where}".strip()
        if not query:
            return False
        data = await self.http.get_json(PHOTON_URL, params={"q": query, "limit": 5, "lang": "en"})
        features = data.get("features") if isinstance(data, dict) else None
        city, region = (b.address.city if b.address else None), (b.address.region if b.address else None)
        if not city and "," in where:
            city, region = (p.strip() for p in where.split(",", 1))
            region = region.split()[0] if region else region
        feat = photon_pick(b.name, city, region, features or [])
        if feat is None:
            return False
        props = feat.get("properties") or {}
        changed = False
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if b.location is None and len(coords) >= 2:
            b.location = GeoPoint(lat=float(coords[1]), lng=float(coords[0]))
            changed = True
        cur = b.address or Address()
        house, street = props.get("housenumber"), props.get("street")
        line1 = " ".join(str(p) for p in (house, street) if p) or None
        if line1 and not cur.line1:
            cur.line1 = line1
            changed = True
        if props.get("city") and not cur.city:
            cur.city = str(props["city"])
            changed = True
        if props.get("postcode") and not cur.postal_code:
            cur.postal_code = str(props["postcode"])
            changed = True
        if changed:
            b.address = cur
            b.attributes["poi_source"] = "photon"
        osm_type, osm_id = props.get("osm_type"), props.get("osm_id")
        if osm_type and osm_id and (not b.website or not b.phone_e164):
            prefix = {"N": "N", "W": "W", "R": "R"}.get(str(osm_type)[0].upper(), "N")
            try:
                rows = await self.http.get_json(
                    f"{self.settings.nominatim_url}/lookup",
                    params={"osm_ids": f"{prefix}{osm_id}", "format": "json", "extratags": 1},
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.photon_lookup_failed", error=str(exc)[:200])
                rows = []
            extra = (rows[0].get("extratags") or {}) if isinstance(rows, list) and rows else {}
            if not b.website:
                url = normalize_url(extra.get("website") or extra.get("contact:website"))
                if url and not is_social_or_directory_url(url):
                    b.website = url
                    b.domain = extract_domain(url)
                    b.attributes["website_source"] = "osm"
                    changed = True
            if not b.phone_e164:
                e164 = normalize_phone(extra.get("phone") or extra.get("contact:phone"))
                if e164:
                    b.phone_e164 = e164
                    changed = True
        return changed

    async def _nominatim(self, b: Business) -> bool:
        query = " ".join(p for p in (b.name, self._where(b)) if p).strip()
        if not query:
            return False
        data = await self.http.get_json(
            f"{self.settings.nominatim_url}/search",
            params={
                "q": query,
                "format": "json",
                "limit": 3,
                "countrycodes": "us",
                "addressdetails": 1,
                "extratags": 1,
            },
        )
        if not isinstance(data, list) or not data:
            return False
        target = normalize_name(b.name)
        best: dict[str, Any] | None = None
        best_score = 0.0
        for row in data:
            display = str(row.get("display_name") or row.get("name") or "")
            score = float(fuzz.token_set_ratio(target, normalize_name(display.split(",")[0])))
            if score > best_score:
                best, best_score = row, score
        if best is None or best_score < NOMINATIM_MATCH_THRESHOLD:
            return False
        extra = best.get("extratags") or {}
        addr = best.get("address") or {}
        changed = False
        if not b.website:
            site = extra.get("website") or extra.get("contact:website")
            url = normalize_url(site) if site else None
            if url and not is_social_or_directory_url(url):
                b.website = url
                b.domain = extract_domain(url)
                b.attributes["website_source"] = "nominatim"
                changed = True
        if not b.phone_e164:
            phone = extra.get("phone") or extra.get("contact:phone")
            e164 = normalize_phone(phone) if phone else None
            if e164:
                b.phone_e164 = e164
                changed = True
        if b.location is None and best.get("lat") and best.get("lon"):
            b.location = GeoPoint(lat=float(best["lat"]), lng=float(best["lon"]))
            changed = True
        cur = b.address or Address()
        if not cur.line1:
            house = addr.get("house_number")
            street = addr.get("road")
            line1 = " ".join(p for p in (house, street) if p) or None
            if line1:
                cur.line1 = line1
                changed = True
        if not cur.city and (addr.get("city") or addr.get("town") or addr.get("village")):
            cur.city = addr.get("city") or addr.get("town") or addr.get("village")
            changed = True
        if not cur.region and addr.get("state"):
            cur.region = addr.get("ISO3166-2-lvl4", "").split("-")[-1] or cur.region
            changed = True
        if not cur.postal_code and addr.get("postcode"):
            cur.postal_code = addr.get("postcode")
            changed = True
        if changed:
            b.address = cur
        return changed

    async def _duckduckgo(self, b: Business) -> str | None:
        where = self._where(b)
        query = f"{b.name} {where} official website".strip()
        resp = await self.http.get(DDG_URL, params={"q": query}, headers=DDG_HEADERS)
        if resp.status_code >= 400 or not resp.text:
            return None
        urls = parse_ddg_html(resp.text)
        return pick_website(b.name, urls)


def completeness(b: Business) -> tuple[int, ...]:
    """Sort key: crawled sites with phone and street beat empty licence names."""
    addr = b.address
    return (
        int(b.website_features.crawled),
        int(bool(b.website)),
        int(bool(b.phone_e164)),
        int(bool(addr and addr.line1)),
        int(bool(addr and addr.city)),
        int(bool(b.owner.name)),
        int(bool(b.review_count)),
    )
