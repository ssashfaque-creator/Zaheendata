"""Free discovery of website, address and phone before Google Places runs.

Used in the enrich stage so a registry-only long list (often just a legal name) can still be
crawled and scored. Census batch geocodes streets in one call; Photon finds the OSM POI;
Overpass pulls website/phone tags for those OSM ids together; DuckDuckGo fills remaining
websites. Nominatim is a fail-fast last resort only.
"""

from __future__ import annotations

import asyncio
import csv
import io
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from ..http import HostPaused, HttpClient
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
PHOTON_URL = "https://photon.komoot.io/api/"
CENSUS_GEOCODER_URL = "https://geocoding.geo.census.gov/geocoder/locations/address"
CENSUS_BATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"
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
        # Photon and Census can run several at once. DuckDuckGo stays at 1 conn/host; we split
        # html vs lite so website search is ~2/s without doubling up on one endpoint.
        self.http.set_host_rate("html.duckduckgo.com", 1.0, concurrency=1)
        self.http.set_host_rate("lite.duckduckgo.com", 1.0, concurrency=1)
        self.http.set_host_rate("photon.komoot.io", 3.0, concurrency=3)
        self.http.set_host_rate("geocoding.geo.census.gov", 4.0, concurrency=4)
        host = urlparse(self.settings.nominatim_url).netloc
        if host:
            self.http.set_host_rate(host, 1.0, concurrency=1)

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
        """Best-effort fill. Census, Photon and DuckDuckGo run together; Nominatim is last resort."""
        found: dict[str, str] = {}

        async def census() -> None:
            try:
                if await self._census(b):
                    found["geo"] = "census"
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.census_failed", name=b.name, error=str(exc)[:200])

        async def photon() -> None:
            try:
                if await self._photon(b):
                    found["poi"] = "photon"
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.photon_failed", name=b.name, error=str(exc)[:200])

        async def ddg() -> None:
            if await self.fill_website(b):
                found["website"] = b.attributes.get("website_source") or "duckduckgo"

        await asyncio.gather(census(), photon(), ddg(), return_exceptions=True)
        # Photon already queried OSM. Nominatim is only for rows Photon missed entirely.
        if (
            not b.website
            and not b.phone_e164
            and not b.attributes.get("osm_id")
            and not self.http.is_paused(urlparse(self.settings.nominatim_url).netloc)
        ):
            try:
                if await self._nominatim(b):
                    found.setdefault("geo", "nominatim")
                    if b.website:
                        found.setdefault("website", "nominatim")
            except HostPaused:
                pass
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.nominatim_failed", name=b.name, error=str(exc)[:200])
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
            return filled
        if resp.status_code >= 400:
            log.debug("discover.census_batch_status", status=resp.status_code)
            return 0
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
        return n

    async def fill_photon_many(self, businesses: list[Business]) -> int:
        filled = 0

        async def one(b: Business) -> None:
            nonlocal filled
            try:
                if await self._photon(b):
                    filled += 1
            except Exception as exc:  # noqa: BLE001
                log.debug("discover.photon_failed", name=b.name, error=str(exc)[:200])

        await asyncio.gather(*(one(b) for b in businesses), return_exceptions=True)
        return filled

    async def fill_website(self, b: Business) -> bool:
        if b.website:
            return False
        try:
            url = await self._duckduckgo(b)
        except HostPaused:
            return False
        except Exception as exc:  # noqa: BLE001
            log.debug("discover.ddg_failed", name=b.name, error=str(exc)[:200])
            return False
        if not url:
            return False
        b.website = url
        b.domain = extract_domain(url)
        b.attributes["website_source"] = "duckduckgo"
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
        urls = [self.settings.overpass_url, *self.settings.overpass_fallback_urls]
        last: Exception | None = None
        for url in urls:
            host = urlparse(url).netloc
            if host:
                self.http.set_host_rate(host, 0.3, concurrency=1)
            if self.http.is_paused(host):
                continue
            try:
                data = await self.http.post_json(url, data={"data": query}, retries=1, timeout_seconds=20.0)
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

    async def _photon(self, b: Business) -> bool:
        where = self._where(b)
        query = f"{b.name} {where}".strip()
        if not query:
            return False
        data = await self.http.get_json(
            PHOTON_URL, params={"q": query, "limit": 5, "lang": "en"}, **self._fast()
        )
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
        osm_type, osm_id = props.get("osm_type"), props.get("osm_id")
        if osm_type and osm_id:
            b.attributes["osm_type"] = str(osm_type)
            b.attributes["osm_id"] = osm_id
        if changed:
            b.address = cur
            b.attributes["poi_source"] = "photon"
        return bool(changed or osm_id)

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
            **self._fast(),
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
        primary = DDG_URL if hash(b.name) % 2 == 0 else DDG_LITE_URL
        other = DDG_LITE_URL if primary == DDG_URL else DDG_URL
        for url in (primary, other):
            host = urlparse(url).netloc
            if self.http.is_paused(host):
                continue
            try:
                resp = await self.http.get(url, params={"q": query}, headers=DDG_HEADERS, **self._fast())
            except HostPaused:
                continue
            if resp.status_code >= 400 or not resp.text:
                continue
            picked = pick_website(b.name, parse_ddg_html(resp.text))
            if picked:
                return picked
        return None


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
