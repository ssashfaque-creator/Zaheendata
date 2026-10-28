"""Free discovery of website, address and phone before Google Places runs.

Used in the enrich stage so a registry-only long list (often just a legal name) can still be
crawled and scored. Photon/Nominatim public endpoints time out from many networks, so enrich
never calls them. Census batch geocodes streets; one Overpass query matches OSM POIs by tag
and by name; ArcGIS World Geocoding fills remaining POI/address gaps; Bing/Brave/Yahoo/DDG
and crt.sh find websites. Every company is processed — a dead host skips that host, not the row.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from ..geo import known_place
from ..http import HostPaused, HostUnavailable, HttpClient
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
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
BING_URL = "https://www.bing.com/search"
BRAVE_URL = "https://search.brave.com/search"
YAHOO_URL = "https://search.yahoo.com/search"
ARCGIS_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
CRTSH_URL = "https://crt.sh/"
WEBSITE_MATCH_THRESHOLD = 55
PHOTON_MATCH_THRESHOLD = 85
ARCGIS_SCORE_MIN = 80
US_PHONE_RE = re.compile(r"(?:\+1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}")
_OVERPASS_SAFE = re.compile(r"[^A-Za-z0-9 ]+")
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
    "brave.com",
    "search.brave.com",
    "yahoo.com",
    "search.yahoo.com",
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


def parse_ddg_api(payload: dict[str, Any]) -> list[str]:
    """URLs from DuckDuckGo's JSON API (different host than the HTML endpoints)."""
    urls: list[str] = []
    for key in ("AbstractURL", "Redirect"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.startswith("http"):
            urls.append(raw)
    for item in payload.get("Results") or []:
        if isinstance(item, dict) and isinstance(item.get("FirstURL"), str):
            urls.append(item["FirstURL"])

    def walk(topics: Any) -> None:
        if not isinstance(topics, list):
            return
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            first = topic.get("FirstURL")
            if isinstance(first, str):
                urls.append(first)
            walk(topic.get("Topics"))

    walk(payload.get("RelatedTopics"))
    out: list[str] = []
    seen: set[str] = set()
    for raw in urls:
        url = normalize_url(raw)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def parse_crtsh(rows: list[Any]) -> list[str]:
    """Hostnames from Certificate Transparency (the company actually requested that cert)."""
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        blobs = [str(row.get("common_name") or ""), str(row.get("name_value") or "")]
        for blob in blobs:
            for part in blob.replace(",", " ").split():
                host = part.strip().lower().lstrip("*.")
                if host.startswith("www."):
                    host = host[4:]
                if "." not in host or " " in host or host.endswith(".local"):
                    continue
                if any(host.endswith(j) for j in (".csv", ".png", ".jpg")):
                    continue
                url = normalize_url("https://" + host)
                if url and url not in seen:
                    seen.add(url)
                    urls.append(url)
    return urls


def parse_bing_html(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a"):
        url = normalize_url(_href_str(a.get("href")))
        if not url or url in seen:
            continue
        host = extract_domain(url) or ""
        if host in {"bing.com", "microsoft.com", "msn.com"} or host.endswith(".bing.com"):
            continue
        seen.add(url)
        urls.append(url)
    return urls


def parse_bing_results(html: str) -> list[tuple[str, str | None]]:
    """Bing SERP rows as (url, phone) so a result can fill both website and phone."""
    soup = BeautifulSoup(html or "", "html.parser")
    rows: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    blocks = soup.select("li.b_algo") or soup.find_all("li")
    for block in blocks:
        a = block.find("a", href=True)
        url = normalize_url(_href_str(a.get("href") if a else None))
        if not url or url in seen:
            continue
        host = extract_domain(url) or ""
        if host in {"bing.com", "microsoft.com", "msn.com"} or host.endswith(".bing.com"):
            continue
        seen.add(url)
        phone: str | None = None
        tel = block.find("a", href=re.compile(r"^tel:", re.I))
        if tel:
            phone = normalize_phone(_href_str(tel.get("href")).replace("tel:", "", 1))
        if not phone:
            match = US_PHONE_RE.search(block.get_text(" ", strip=True))
            if match:
                phone = normalize_phone(match.group(0))
        rows.append((url, phone))
    if not rows:
        rows = [(url, None) for url in parse_bing_html(html)]
    return rows


def parse_arcgis_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Turn ArcGIS findAddressCandidates JSON into the same feature shape Overpass uses."""
    features: list[dict[str, Any]] = []
    for cand in payload.get("candidates") or []:
        if not isinstance(cand, dict):
            continue
        attrs = cand.get("attributes") or {}
        loc = cand.get("location") or {}
        try:
            lng = float(loc.get("x"))
            lat = float(loc.get("y"))
        except (TypeError, ValueError):
            continue
        street = str(attrs.get("StAddr") or "").strip() or None
        house = str(attrs.get("AddNum") or "").strip() or None
        if street and house and street.lower().startswith(house.lower()):
            house = None
        features.append(
            {
                "properties": {
                    "name": str(attrs.get("PlaceName") or cand.get("address") or ""),
                    "city": attrs.get("City"),
                    "state": attrs.get("RegionAbbr") or attrs.get("Region"),
                    "housenumber": house,
                    "street": street,
                    "postcode": attrs.get("Postal"),
                    "website": attrs.get("URL"),
                    "phone": attrs.get("Phone"),
                    "score": float(cand.get("score") or 0),
                },
                "geometry": {"coordinates": [lng, lat]},
            }
        )
    return features


def overpass_name_needle(name: str) -> str | None:
    """Strip legal suffixes and punctuation so an Overpass name regex stays valid."""
    text = _OVERPASS_SAFE.sub(" ", name)
    suffixes = r"\b(INC|INCORPORATED|LLC|LTD|LIMITED|CORP|CORPORATION|CO|COMPANY|PLLC)\b"
    text = re.sub(suffixes, "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 5:
        return None
    return text[:40]


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


def parse_census_batch(text: str) -> dict[str, GeoPoint]:
    """Map unique id -> point from a Census addressbatch CSV body."""
    out: dict[str, GeoPoint] = {}
    if not text or not text.strip():
        return out
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 6:
            continue
        status = (row[2] or "").strip()
        if status.lower() != "match":
            continue
        coords = (row[5] or "").strip()
        if "," not in coords:
            continue
        lon_s, lat_s = (p.strip() for p in coords.split(",", 1))
        try:
            out[row[0].strip()] = GeoPoint(lat=float(lat_s), lng=float(lon_s))
        except ValueError:
            continue
    return out


def _osm_ref(osm_type: object, osm_id: object) -> tuple[str, int] | None:
    kind = {"N": "node", "W": "way", "R": "relation"}.get(str(osm_type or "")[:1].upper())
    try:
        oid = int(osm_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return (kind, oid) if kind else None


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
        # Search engines stay polite; Bing is the primary website source so it gets 2 slots.
        # Photon/Nominatim are intentionally not used — they hang from many networks.
        self.http.set_host_rate("html.duckduckgo.com", 1.0, concurrency=1)
        self.http.set_host_rate("lite.duckduckgo.com", 1.0, concurrency=1)
        self.http.set_host_rate("www.bing.com", 2.0, concurrency=2)
        self.http.set_host_rate("search.brave.com", 1.0, concurrency=1)
        self.http.set_host_rate("search.yahoo.com", 1.0, concurrency=1)
        self.http.set_host_rate("crt.sh", 0.5, concurrency=1)
        self.http.set_host_rate("geocode.arcgis.com", 4.0, concurrency=4)
        self.http.set_host_rate("geocoding.geo.census.gov", 4.0, concurrency=4)

    def _fast(self) -> dict[str, Any]:
        return {
            "timeout_seconds": self.settings.discover_timeout_seconds,
            "retries": self.settings.discover_retries,
        }

    def _where(self, b: Business) -> str:
        addr = b.address
        city = (addr.city if addr else None) or ""
        region = (addr.region if addr else None) or ""
        built = ", ".join(p for p in (city, region) if p)
        return built or self.fallback_where

    async def fill(self, b: Business) -> dict[str, str]:
        """Best-effort fill. Census, ArcGIS POI and website search run together. No Photon."""
        found: dict[str, str] = {}

        async def census() -> None:
            try:
                if await self._census(b):
                    found["geo"] = "census"
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.census_failed", name=b.name, error=str(exc)[:200])

        async def arcgis() -> None:
            try:
                if await self._arcgis(b, poi=True):
                    found["poi"] = "arcgis"
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.arcgis_failed", name=b.name, error=str(exc)[:200])

        async def web() -> None:
            if await self.fill_website(b):
                found["website"] = b.attributes.get("website_source") or "search"

        await asyncio.gather(census(), arcgis(), web(), return_exceptions=True)
        if found:
            log.info("discover.filled", name=b.name, **found)
        return found

    async def geocode_batch(self, businesses: list[Business]) -> int:
        """One Census addressbatch call for every record that has a street and no lat/lng."""
        need = [b for b in businesses if b.location is None and b.address and b.address.line1]
        if not need:
            return 0
        buf = io.StringIO()
        writer = csv.writer(buf)
        for b in need:
            addr = b.address
            assert addr is not None
            writer.writerow(
                [b.id, addr.line1 or "", addr.city or "", addr.region or "", addr.postal_code or ""]
            )
        try:
            resp = await self.http.post(
                CENSUS_BATCH_URL,
                data={"benchmark": "Public_AR_Current"},
                files={"addressFile": ("addresses.csv", buf.getvalue(), "text/csv")},
                timeout_seconds=45.0,
                retries=1,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("discover.census_batch_failed", error=str(exc)[:200])
            filled = 0

            async def one(b: Business) -> None:
                nonlocal filled
                try:
                    if await self._census(b):
                        filled += 1
                except Exception as err:  # noqa: BLE001
                    log.debug("discover.census_failed", name=b.name, error=str(err)[:200])

            await asyncio.gather(*(one(b) for b in need), return_exceptions=True)
            missed = [b for b in need if b.location is None]
            if missed:
                filled += await self._geocode_arcgis_many(missed)
            return filled
        if resp.status_code >= 400:
            log.debug("discover.census_batch_status", status=resp.status_code)
            missed = [b for b in need if b.location is None]
            return await self._geocode_arcgis_many(missed) if missed else 0
        points = parse_census_batch(resp.text)
        n = 0
        for b in need:
            point = points.get(b.id)
            if point is None:
                continue
            b.location = point
            b.attributes["geo_source"] = "census"
            n += 1
        log.info("discover.census_batch", submitted=len(need), matched=n)
        missed = [b for b in need if b.location is None]
        if missed:
            n += await self._geocode_arcgis_many(missed)
        return n

    async def _geocode_arcgis_many(self, businesses: list[Business]) -> int:
        """Street geocode for Census misses. Does not search by business name."""
        filled = 0
        sem = asyncio.Semaphore(4)

        async def one(b: Business) -> None:
            nonlocal filled
            async with sem:
                try:
                    if await self._arcgis(b, poi=False):
                        filled += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("discover.arcgis_geocode_failed", name=b.name, error=str(exc)[:200])

        await asyncio.gather(*(one(b) for b in businesses), return_exceptions=True)
        if filled:
            log.info("discover.arcgis_geocode", filled=filled, submitted=len(businesses))
        return filled

    async def fill_arcgis_many(self, businesses: list[Business]) -> int:
        """POI lookup for rows still missing website, phone, or a street after OSM."""
        need = [
            b
            for b in businesses
            if not b.website or not b.phone_e164 or not (b.address and b.address.line1)
        ]
        if not need:
            return 0
        filled = 0
        sem = asyncio.Semaphore(4)

        async def one(b: Business) -> None:
            nonlocal filled
            async with sem:
                try:
                    if await self._arcgis(b, poi=True):
                        filled += 1
                except Exception as exc:  # noqa: BLE001
                    log.debug("discover.arcgis_failed", name=b.name, error=str(exc)[:200])

        await asyncio.gather(*(one(b) for b in need), return_exceptions=True)
        if filled:
            log.info("discover.arcgis_poi", filled=filled, submitted=len(need))
        return filled

    async def geocode_place(self, text: str) -> GeoPoint | None:
        """Geocode a city/metro string via ArcGIS, then a built-in table. No Nominatim."""
        if not (text or "").strip():
            return None
        host = urlparse(ARCGIS_URL).netloc
        if host and self.http.host_available(host):
            try:
                data = await self.http.get_json(
                    ARCGIS_URL,
                    params={
                        "f": "json",
                        "SingleLine": text,
                        "maxLocations": 1,
                        "outFields": "City,Region,RegionAbbr,Postal",
                        "countryCode": "USA",
                    },
                    **self._fast(),
                )
                feats = parse_arcgis_candidates(data if isinstance(data, dict) else {})
                if feats:
                    coords = (feats[0].get("geometry") or {}).get("coordinates") or []
                    if len(coords) >= 2:
                        return GeoPoint(lat=float(coords[1]), lng=float(coords[0]))
            except (HostPaused, HostUnavailable):
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.place_geocode_failed", error=str(exc)[:200])
        return known_place(text)

    async def match_overpass(
        self,
        businesses: list[Business],
        osm_tags: list[str],
        lat: float,
        lng: float,
        radius_km: float,
    ) -> int:
        """One Overpass query for the metro; match POIs onto the long list by name + city."""
        if not businesses or not osm_tags:
            return 0
        clauses: list[str] = []
        radius_m = max(1000, int(radius_km * 1000))
        for tag in osm_tags:
            key, _, value = tag.partition("=")
            clauses.append(f'nwr["{key}"="{value}"](around:{radius_m},{lat},{lng});')
        query = f"[out:json][timeout:25];({''.join(clauses)});out center tags 800;"
        data = await self._overpass_query(query)
        features: list[dict[str, Any]] = []
        for el in data.get("elements") or []:
            tags = el.get("tags") or {}
            name = tags.get("name")
            if not name:
                continue
            plat = el.get("lat") or (el.get("center") or {}).get("lat")
            plng = el.get("lon") or (el.get("center") or {}).get("lon")
            features.append(
                {
                    "properties": {
                        "name": name,
                        "city": tags.get("addr:city"),
                        "state": tags.get("addr:state"),
                        "housenumber": tags.get("addr:housenumber"),
                        "street": tags.get("addr:street"),
                        "postcode": tags.get("addr:postcode"),
                        "osm_type": {"node": "N", "way": "W", "relation": "R"}.get(str(el.get("type")), "N"),
                        "osm_id": el.get("id"),
                        "website": tags.get("website") or tags.get("contact:website"),
                        "phone": tags.get("phone") or tags.get("contact:phone"),
                    },
                    "geometry": {"coordinates": [float(plng or 0), float(plat or 0)]},
                }
            )
        filled = self._assign_osm_features(businesses, features)
        if filled:
            log.info("discover.overpass_matched", filled=filled, pois=len(features))
        remaining = [b for b in businesses if not b.website or not b.phone_e164]
        if remaining:
            filled += await self._match_overpass_names(remaining, lat, lng, radius_km)
        return filled

    def _assign_osm_features(self, businesses: list[Business], features: list[dict[str, Any]]) -> int:
        filled = 0
        available = features
        for b in businesses:
            city = b.address.city if b.address else None
            region = b.address.region if b.address else None
            feat = photon_pick(b.name, city, region, available)
            if feat is None:
                continue
            available = [f for f in available if f is not feat]
            if self._apply_osm_feature(b, feat):
                filled += 1
        return filled

    async def _match_overpass_names(
        self, businesses: list[Business], lat: float, lng: float, radius_km: float
    ) -> int:
        """Second Overpass pass: match remaining rows by business name, not craft= tag."""
        radius_m = max(1000, int(radius_km * 1000))
        needles: list[tuple[Business, str]] = []
        seen: set[str] = set()
        for b in businesses:
            needle = overpass_name_needle(b.name)
            if not needle or needle.lower() in seen:
                continue
            seen.add(needle.lower())
            needles.append((b, needle))
        if not needles:
            return 0
        filled = 0
        for i in range(0, len(needles), 12):
            chunk = needles[i : i + 12]
            pattern = "|".join(n for _, n in chunk)
            query = (
                f'[out:json][timeout:20];(nwr["name"~"{pattern}",i](around:{radius_m},{lat},{lng}););'
                "out center tags 400;"
            )
            data = await self._overpass_query(query)
            features: list[dict[str, Any]] = []
            for el in data.get("elements") or []:
                tags = el.get("tags") or {}
                name = tags.get("name")
                if not name:
                    continue
                plat = el.get("lat") or (el.get("center") or {}).get("lat")
                plng = el.get("lon") or (el.get("center") or {}).get("lon")
                features.append(
                    {
                        "properties": {
                            "name": name,
                            "city": tags.get("addr:city"),
                            "state": tags.get("addr:state"),
                            "housenumber": tags.get("addr:housenumber"),
                            "street": tags.get("addr:street"),
                            "postcode": tags.get("addr:postcode"),
                            "osm_type": {"node": "N", "way": "W", "relation": "R"}.get(
                                str(el.get("type")), "N"
                            ),
                            "osm_id": el.get("id"),
                            "website": tags.get("website") or tags.get("contact:website"),
                            "phone": tags.get("phone") or tags.get("contact:phone"),
                        },
                        "geometry": {"coordinates": [float(plng or 0), float(plat or 0)]},
                    }
                )
            n = self._assign_osm_features([b for b, _ in chunk], features)
            filled += n
        if filled:
            log.info("discover.overpass_names", filled=filled)
        return filled

    def _apply_osm_feature(self, b: Business, feat: dict[str, Any]) -> bool:
        props = feat.get("properties") or {}
        changed = False
        coords = (feat.get("geometry") or {}).get("coordinates") or []
        if b.location is None and len(coords) >= 2 and (coords[0] or coords[1]):
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
        if props.get("osm_type") and props.get("osm_id"):
            b.attributes["osm_type"] = str(props["osm_type"])
            b.attributes["osm_id"] = props["osm_id"]
        if not b.website:
            url = normalize_url(props.get("website"))
            if url and not is_social_or_directory_url(url):
                b.website = url
                b.domain = extract_domain(url)
                b.attributes["website_source"] = "osm"
                changed = True
        if not b.phone_e164:
            e164 = normalize_phone(props.get("phone"))
            if e164:
                b.phone_e164 = e164
                changed = True
        if changed:
            b.attributes["poi_source"] = "overpass"
        return changed

    async def fill_website(self, b: Business) -> bool:
        if b.website:
            return False
        try:
            url, phone, source = await self._search_website(b)
        except HostPaused:
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("discover.search_failed", name=b.name, error=str(exc)[:200])
            return False
        if not url:
            return False
        b.website = url
        b.domain = extract_domain(url)
        b.attributes["website_source"] = source
        if phone and not b.phone_e164:
            b.phone_e164 = phone
        return True

    async def apply_osm_contacts(self, businesses: list[Business]) -> int:
        """One Overpass lookup for Photon OSM ids — website/phone tags without per-row Nominatim."""
        by_ref: dict[tuple[str, int], list[Business]] = {}
        for b in businesses:
            if b.website and b.phone_e164:
                continue
            ref = _osm_ref(b.attributes.get("osm_type"), b.attributes.get("osm_id"))
            if ref is None:
                continue
            by_ref.setdefault(ref, []).append(b)
        if not by_ref:
            return 0
        refs = list(by_ref)
        filled = 0
        for i in range(0, len(refs), 80):
            chunk = refs[i : i + 80]
            data = await self._overpass_ids(chunk)
            for el in data.get("elements") or []:
                kind = str(el.get("type") or "")
                try:
                    oid = int(el.get("id"))
                except (TypeError, ValueError):
                    continue
                tags = el.get("tags") or {}
                for b in by_ref.get((kind, oid), []):
                    changed = False
                    if not b.website:
                        url = normalize_url(tags.get("website") or tags.get("contact:website"))
                        if url and not is_social_or_directory_url(url):
                            b.website = url
                            b.domain = extract_domain(url)
                            b.attributes["website_source"] = "osm"
                            changed = True
                    if not b.phone_e164:
                        e164 = normalize_phone(tags.get("phone") or tags.get("contact:phone"))
                        if e164:
                            b.phone_e164 = e164
                            changed = True
                    if changed:
                        filled += 1
        if filled:
            log.info("discover.osm_contacts", filled=filled, refs=len(refs))
        return filled

    async def _overpass_ids(self, refs: list[tuple[str, int]]) -> dict[str, Any]:
        parts: list[str] = []
        for kind, group_key in (("node", "node"), ("way", "way"), ("relation", "relation")):
            ids = [str(oid) for k, oid in refs if k == group_key]
            if ids:
                parts.append(f"{kind}(id:{','.join(ids)});")
        if not parts:
            return {}
        query = f"[out:json][timeout:20];({''.join(parts)});out tags;"
        return await self._overpass_query(query)

    async def _overpass_query(self, query: str) -> dict[str, Any]:
        urls = [self.settings.overpass_url, *self.settings.overpass_fallback_urls]
        last: Exception | None = None
        for url in urls:
            host = urlparse(url).netloc
            if host:
                self.http.set_host_rate(host, 0.3, concurrency=1)
            if host and not self.http.host_available(host):
                continue
            try:
                data = await self.http.post_json(url, data={"data": query}, retries=1, timeout_seconds=25.0)
            except (HostPaused, HostUnavailable) as exc:
                last = exc
                continue
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.debug("discover.overpass_failed", url=url, error=str(exc)[:200])
                continue
            return data if isinstance(data, dict) else {}
        if last:
            log.warning("discover.overpass_giveup", error=str(last)[:200])
        return {}

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
            **self._fast(),
        )
        point = census_point(data if isinstance(data, dict) else {})
        if point is None:
            return False
        b.location = point
        b.attributes["geo_source"] = "census"
        return True

    async def _arcgis(self, b: Business, *, poi: bool) -> bool:
        host = urlparse(ARCGIS_URL).netloc
        if host and not self.http.host_available(host):
            return False
        if poi:
            query = f"{b.name} {self._where(b)}".strip()
            if not query:
                return False
            params: dict[str, Any] = {
                "f": "json",
                "SingleLine": query,
                "maxLocations": 5,
                "outFields": (
                    "PlaceName,Place_addr,StAddr,AddNum,City,Region,RegionAbbr,Postal,Phone,URL,Type"
                ),
                "countryCode": "USA",
                "category": "POI",
            }
        else:
            addr = b.address
            if not addr or not addr.line1:
                return False
            line = ", ".join(p for p in (addr.line1, addr.city, addr.region, addr.postal_code) if p)
            params = {
                "f": "json",
                "SingleLine": line,
                "maxLocations": 1,
                "outFields": "StAddr,AddNum,City,Region,RegionAbbr,Postal",
                "countryCode": "USA",
            }
        try:
            data = await self.http.get_json(ARCGIS_URL, params=params, **self._fast())
        except (HostPaused, HostUnavailable):
            return False
        features = parse_arcgis_candidates(data if isinstance(data, dict) else {})
        if not features:
            return False
        if not poi:
            feat = features[0]
            score = float((feat.get("properties") or {}).get("score") or 0)
            if score and score < ARCGIS_SCORE_MIN:
                return False
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if b.location is None and len(coords) >= 2 and (coords[0] or coords[1]):
                b.location = GeoPoint(lat=float(coords[1]), lng=float(coords[0]))
                b.attributes["geo_source"] = "arcgis"
                return True
            return False
        city = b.address.city if b.address else None
        region = b.address.region if b.address else None
        feat = photon_pick(b.name, city, region, features)
        if feat is None:
            return False
        score = float((feat.get("properties") or {}).get("score") or 0)
        if score and score < ARCGIS_SCORE_MIN:
            return False
        changed = self._apply_osm_feature(b, feat)
        if changed:
            b.attributes["poi_source"] = "arcgis"
        return changed

    async def _search_website(self, b: Business) -> tuple[str | None, str | None, str]:
        where = self._where(b)
        query = f"{b.name} {where} official website".strip()
        engines: list[tuple[str, str, str]] = [
            ("bing", BING_URL, "bing"),
            ("brave", BRAVE_URL, "html"),
            ("yahoo", YAHOO_URL, "html"),
            ("duckduckgo", DDG_URL, "html"),
            ("duckduckgo", DDG_LITE_URL, "html"),
            ("crtsh", CRTSH_URL, "crtsh"),
        ]
        for source, url, kind in engines:
            host = urlparse(url).netloc
            if host and not self.http.host_available(host):
                continue
            phones: dict[str, str] = {}
            try:
                if kind == "bing":
                    resp = await self.http.get(
                        url, params={"q": query}, headers=DDG_HEADERS, **self._fast()
                    )
                    rows = parse_bing_results(resp.text if resp.status_code < 400 else "")
                    urls = [item[0] for item in rows]
                    phones = {item[0]: item[1] for item in rows if item[1]}
                elif kind == "crtsh":
                    resp = await self.http.get(
                        url, params={"q": b.name, "output": "json"}, **self._fast()
                    )
                    try:
                        payload = resp.json() if resp.status_code < 400 else []
                    except Exception:  # noqa: BLE001
                        payload = []
                    urls = parse_crtsh(payload if isinstance(payload, list) else [])
                else:
                    param = "p" if "yahoo.com" in host else "q"
                    resp = await self.http.get(
                        url, params={param: query}, headers=DDG_HEADERS, **self._fast()
                    )
                    if resp.status_code >= 400 or not resp.text:
                        continue
                    urls = parse_ddg_html(resp.text)
            except (HostPaused, HostUnavailable, Exception) as exc:  # noqa: BLE001
                log.debug("discover.search_failed", engine=source, url=url, error=str(exc)[:200])
                continue
            picked = pick_website(b.name, urls)
            if picked:
                b.attributes["website_search"] = source
                return picked, phones.get(picked), source
        return None, None, ""


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
