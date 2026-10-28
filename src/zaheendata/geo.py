"""Geography planning: turn a GeographySpec into concrete search cells, geocode locations,
compute distances, and decide whether a business is inside the target area."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .http import HttpClient
from .logging import get_logger
from .models import GeographySpec, GeoPoint, GeoScope
from .settings import get_settings

log = get_logger(__name__)

# state -> (name, min_lat, min_lng, max_lat, max_lng)
US_STATES: dict[str, tuple[str, float, float, float, float]] = {
    "AL": ("Alabama", 30.14, -88.47, 35.01, -84.89),
    "AK": ("Alaska", 54.40, -170.0, 71.39, -130.0),
    "AZ": ("Arizona", 31.33, -114.82, 37.00, -109.05),
    "AR": ("Arkansas", 33.00, -94.62, 36.50, -89.64),
    "CA": ("California", 32.53, -124.41, 42.01, -114.13),
    "CO": ("Colorado", 36.99, -109.06, 41.00, -102.04),
    "CT": ("Connecticut", 40.95, -73.73, 42.05, -71.79),
    "DE": ("Delaware", 38.45, -75.79, 39.84, -75.05),
    "DC": ("District of Columbia", 38.79, -77.12, 38.99, -76.91),
    "FL": ("Florida", 24.52, -87.63, 31.00, -80.03),
    "GA": ("Georgia", 30.36, -85.61, 35.00, -80.84),
    "HI": ("Hawaii", 18.91, -160.25, 22.24, -154.81),
    "ID": ("Idaho", 41.99, -117.24, 49.00, -111.04),
    "IL": ("Illinois", 36.97, -91.51, 42.51, -87.50),
    "IN": ("Indiana", 37.77, -88.10, 41.76, -84.78),
    "IA": ("Iowa", 40.38, -96.64, 43.50, -90.14),
    "KS": ("Kansas", 36.99, -102.05, 40.00, -94.59),
    "KY": ("Kentucky", 36.50, -89.57, 39.15, -81.96),
    "LA": ("Louisiana", 28.93, -94.04, 33.02, -88.82),
    "ME": ("Maine", 42.98, -71.08, 47.46, -66.95),
    "MD": ("Maryland", 37.91, -79.49, 39.72, -75.05),
    "MA": ("Massachusetts", 41.24, -73.51, 42.89, -69.93),
    "MI": ("Michigan", 41.70, -90.42, 48.31, -82.41),
    "MN": ("Minnesota", 43.50, -97.24, 49.38, -89.49),
    "MS": ("Mississippi", 30.17, -91.66, 35.00, -88.10),
    "MO": ("Missouri", 35.99, -95.77, 40.61, -89.10),
    "MT": ("Montana", 44.36, -116.05, 49.00, -104.04),
    "NE": ("Nebraska", 39.99, -104.05, 43.00, -95.31),
    "NV": ("Nevada", 35.00, -120.01, 42.00, -114.04),
    "NH": ("New Hampshire", 42.70, -72.56, 45.31, -70.70),
    "NJ": ("New Jersey", 38.93, -75.56, 41.36, -73.89),
    "NM": ("New Mexico", 31.33, -109.05, 37.00, -103.00),
    "NY": ("New York", 40.50, -79.76, 45.02, -71.86),
    "NC": ("North Carolina", 33.84, -84.32, 36.59, -75.46),
    "ND": ("North Dakota", 45.94, -104.05, 49.00, -96.55),
    "OH": ("Ohio", 38.40, -84.82, 41.98, -80.52),
    "OK": ("Oklahoma", 33.62, -103.00, 37.00, -94.43),
    "OR": ("Oregon", 41.99, -124.57, 46.29, -116.46),
    "PA": ("Pennsylvania", 39.72, -80.52, 42.27, -74.69),
    "RI": ("Rhode Island", 41.15, -71.86, 42.02, -71.12),
    "SC": ("South Carolina", 32.03, -83.35, 35.22, -78.54),
    "SD": ("South Dakota", 42.48, -104.06, 45.95, -96.44),
    "TN": ("Tennessee", 34.98, -90.31, 36.68, -81.65),
    "TX": ("Texas", 25.84, -106.65, 36.50, -93.51),
    "UT": ("Utah", 36.99, -114.05, 42.00, -109.04),
    "VT": ("Vermont", 42.73, -73.44, 45.02, -71.46),
    "VA": ("Virginia", 36.54, -83.68, 39.47, -75.24),
    "WA": ("Washington", 45.54, -124.85, 49.00, -116.92),
    "WV": ("West Virginia", 37.20, -82.64, 40.64, -77.72),
    "WI": ("Wisconsin", 42.49, -92.89, 47.08, -86.25),
    "WY": ("Wyoming", 40.99, -111.06, 45.01, -104.05),
}

STATE_NAME_TO_CODE = {name.lower(): code for code, (name, *_rest) in US_STATES.items()}
EARTH_RADIUS_KM = 6371.0088


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, (a.lat, a.lng, b.lat, b.lng))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def normalize_state(value: str | None) -> str | None:
    if not value:
        return None
    v = value.strip()
    if len(v) == 2 and v.upper() in US_STATES:
        return v.upper()
    return STATE_NAME_TO_CODE.get(v.lower())


def parse_location_text(text: str) -> tuple[str, str | None]:
    """'Austin, TX' -> ('Austin', 'TX'); 'Austin Texas' -> ('Austin', 'TX')."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) >= 2:
        return parts[0], normalize_state(parts[1])
    m = re.match(r"^(.*)\s+([A-Za-z]{2}|[A-Za-z ]+)$", text.strip())
    if m and normalize_state(m.group(2)):
        return m.group(1).strip(), normalize_state(m.group(2))
    return text.strip(), None


@dataclass(frozen=True)
class SearchCell:
    center: GeoPoint
    radius_km: float
    label: str
    region: str | None


def grid_cells(
    min_lat: float,
    min_lng: float,
    max_lat: float,
    max_lng: float,
    cell_km: float,
    label: str,
    region: str | None,
) -> list[SearchCell]:
    cells: list[SearchCell] = []
    lat_step = cell_km / 111.0
    lat = min_lat + lat_step / 2
    radius = cell_km / math.sqrt(2)
    while lat < max_lat:
        lng_step = cell_km / (111.0 * max(math.cos(math.radians(lat)), 0.2))
        lng = min_lng + lng_step / 2
        while lng < max_lng:
            cells.append(
                SearchCell(GeoPoint(lat=round(lat, 4), lng=round(lng, 4)), round(radius, 2), label, region)
            )
            lng += lng_step
        lat += lat_step
    if not cells:  # area smaller than one cell (RI, DE, DC ...): search from its centroid
        center = GeoPoint(lat=round((min_lat + max_lat) / 2, 4), lng=round((min_lng + max_lng) / 2, 4))
        cells.append(SearchCell(center, round(radius, 2), label, region))
    return cells


class Geocoder:
    """Google Geocoding when a key is configured, otherwise Nominatim (OSM)."""

    def __init__(self, http: HttpClient):
        self.http = http
        self.settings = get_settings()

    async def geocode(self, text: str) -> GeoPoint | None:
        key = self.settings.secret("google_maps_api_key")
        if key:
            data = await self.http.get_json(
                "https://maps.googleapis.com/maps/api/geocode/json",
                params={"address": text, "key": key},
                meter="google_places.geocode",
            )
            results = data.get("results") or []
            if results:
                loc = results[0]["geometry"]["location"]
                return GeoPoint(lat=loc["lat"], lng=loc["lng"])
            return None
        data = await self.http.get_json(
            f"{self.settings.nominatim_url}/search",
            params={"q": text, "format": "json", "limit": 1, "countrycodes": "us"},
        )
        if data:
            return GeoPoint(lat=float(data[0]["lat"]), lng=float(data[0]["lon"]))
        return None


class GeoPlanner:
    def __init__(self, geocoder: Geocoder | None):
        self.geocoder = geocoder

    async def plan(self, spec: GeographySpec) -> list[SearchCell]:
        if spec.scope == GeoScope.METRO:
            cells: list[SearchCell] = []
            for loc in spec.locations:
                city, state = parse_location_text(loc)
                point = await self.geocoder.geocode(loc) if self.geocoder else None
                if point is None:
                    log.warning("geo.unresolved_location", location=loc)
                    cells.append(SearchCell(GeoPoint(lat=0, lng=0), spec.radius_km, loc, state))
                    continue
                if spec.radius_km <= spec.grid_cell_km:
                    cells.append(SearchCell(point, spec.radius_km, loc, state))
                else:
                    d = spec.radius_km / 111.0
                    cells.extend(
                        grid_cells(
                            point.lat - d,
                            point.lng - d,
                            point.lat + d,
                            point.lng + d,
                            spec.grid_cell_km,
                            loc,
                            state,
                        )
                    )
            return cells
        if spec.scope == GeoScope.STATE:
            state = normalize_state(spec.state)
            if not state:
                raise ValueError(f"Unknown state '{spec.state}'")
            name, min_lat, min_lng, max_lat, max_lng = US_STATES[state]
            return grid_cells(min_lat, min_lng, max_lat, max_lng, spec.grid_cell_km, name, state)
        cells = []
        for code, (name, min_lat, min_lng, max_lat, max_lng) in US_STATES.items():
            cells.extend(
                grid_cells(min_lat, min_lng, max_lat, max_lng, max(spec.grid_cell_km, 60.0), name, code)
            )
        return cells


class AreaMatcher:
    """Decides whether a business is inside the campaign's target area."""

    def __init__(self, spec: GeographySpec, cells: list[SearchCell]):
        self.spec = spec
        self.cells = cells
        self.states = {c.region for c in cells if c.region}
        if spec.state:
            self.states.add(normalize_state(spec.state) or spec.state)

    def inside(self, location: GeoPoint | None, region: str | None) -> tuple[bool, str | None]:
        if self.spec.scope == GeoScope.NATIONAL:
            return True, None
        state = normalize_state(region)
        if self.spec.scope == GeoScope.STATE:
            if state and state not in self.states:
                return False, f"state {state} not in {sorted(self.states)}"
            return True, None  # unknown state: keep, let later stages decide
        if location is not None:
            for cell in self.cells:
                if cell.center.lat == 0 and cell.center.lng == 0:
                    continue
                if haversine_km(location, cell.center) <= cell.radius_km * 1.15:
                    return True, None
            if any(c.center.lat != 0 for c in self.cells):
                return False, "outside metro radius"
        if state and self.states and state not in self.states:
            return False, f"state {state} not in {sorted(self.states)}"
        return True, None
