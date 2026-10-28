from __future__ import annotations

# WGS84 bounding boxes used to slice Overture without downloading the globe.
# Values are (west, south, east, north).

US_BBOX = (-124.85, 24.40, -66.88, 49.38)

STATE_BBOX: dict[str, tuple[float, float, float, float]] = {
    "AL": (-88.47, 30.22, -84.89, 35.01),
    "AK": (-179.15, 51.21, -129.98, 71.44),
    "AZ": (-114.82, 31.33, -109.04, 37.00),
    "AR": (-94.62, 33.00, -89.64, 36.50),
    "CA": (-124.48, 32.53, -114.13, 42.01),
    "CO": (-109.06, 36.99, -102.04, 41.00),
    "CT": (-73.73, 40.98, -71.79, 42.05),
    "DE": (-75.79, 38.45, -75.05, 39.84),
    "DC": (-77.12, 38.79, -76.91, 38.99),
    "FL": (-87.63, 24.40, -80.03, 31.00),
    "GA": (-85.61, 30.36, -80.84, 35.00),
    "HI": (-160.32, 18.91, -154.81, 22.24),
    "ID": (-117.24, 41.99, -111.04, 49.00),
    "IL": (-91.51, 36.97, -87.02, 42.51),
    "IN": (-88.10, 37.77, -84.78, 41.76),
    "IA": (-96.64, 40.38, -90.14, 43.50),
    "KS": (-102.05, 36.99, -94.59, 40.00),
    "KY": (-89.57, 36.50, -81.96, 39.15),
    "LA": (-94.04, 28.93, -88.82, 33.02),
    "ME": (-71.08, 43.06, -66.95, 47.46),
    "MD": (-79.49, 37.91, -75.05, 39.72),
    "MA": (-73.51, 41.24, -69.93, 42.89),
    "MI": (-90.42, 41.70, -82.12, 48.31),
    "MN": (-97.24, 43.50, -89.49, 49.38),
    "MS": (-91.66, 30.17, -88.10, 35.00),
    "MO": (-95.77, 35.99, -89.10, 40.61),
    "MT": (-116.05, 44.36, -104.04, 49.00),
    "NE": (-104.05, 39.99, -95.31, 43.00),
    "NV": (-120.01, 35.00, -114.04, 42.00),
    "NH": (-72.56, 42.70, -70.61, 45.31),
    "NJ": (-75.56, 38.93, -73.89, 41.36),
    "NM": (-109.05, 31.33, -103.00, 37.00),
    "NY": (-79.76, 40.50, -71.86, 45.02),
    "NC": (-84.32, 33.84, -75.46, 36.59),
    "ND": (-104.05, 45.94, -96.55, 49.00),
    "OH": (-84.82, 38.40, -80.52, 42.32),
    "OK": (-103.00, 33.62, -94.43, 37.00),
    "OR": (-124.57, 41.99, -116.46, 46.29),
    "PA": (-80.52, 39.72, -74.69, 42.27),
    "RI": (-71.86, 41.15, -71.12, 42.02),
    "SC": (-83.35, 32.03, -78.54, 35.22),
    "SD": (-104.06, 42.48, -96.44, 45.94),
    "TN": (-90.31, 34.98, -81.65, 36.68),
    "TX": (-106.65, 25.84, -93.51, 36.50),
    "UT": (-114.05, 36.99, -109.04, 42.00),
    "VT": (-73.44, 42.73, -71.47, 45.02),
    "VA": (-83.68, 36.54, -75.24, 39.47),
    "WA": (-124.76, 45.54, -116.92, 49.00),
    "WV": (-82.64, 37.20, -77.72, 40.64),
    "WI": (-92.89, 42.49, -86.25, 47.31),
    "WY": (-111.06, 40.99, -104.05, 45.01),
}

CITY_BBOX: dict[tuple[str, str], tuple[float, float, float, float]] = {
    ("austin", "tx"): (-98.05, 30.10, -97.55, 30.55),
    ("dallas", "tx"): (-97.10, 32.55, -96.45, 33.05),
    ("houston", "tx"): (-95.90, 29.50, -95.00, 30.15),
    ("san antonio", "tx"): (-98.75, 29.25, -98.20, 29.65),
    ("new york", "ny"): (-74.26, 40.49, -73.70, 40.92),
    ("los angeles", "ca"): (-118.67, 33.70, -118.07, 34.34),
    ("chicago", "il"): (-87.94, 41.64, -87.52, 42.02),
    ("miami", "fl"): (-80.48, 25.60, -80.05, 25.96),
    ("phoenix", "az"): (-112.32, 33.29, -111.79, 33.76),
    ("denver", "co"): (-105.15, 39.61, -104.71, 39.91),
    ("seattle", "wa"): (-122.44, 47.49, -122.22, 47.73),
    ("atlanta", "ga"): (-84.55, 33.64, -84.28, 33.89),
    ("boston", "ma"): (-71.19, 42.23, -70.92, 42.40),
    ("philadelphia", "pa"): (-75.28, 39.87, -75.04, 40.05),
    ("detroit", "mi"): (-83.29, 42.25, -82.91, 42.45),
    ("minneapolis", "mn"): (-93.35, 44.89, -93.19, 45.05),
    ("portland", "or"): (-122.84, 45.43, -122.47, 45.65),
    ("las vegas", "nv"): (-115.33, 36.06, -114.93, 36.33),
    ("charlotte", "nc"): (-81.03, 35.10, -80.70, 35.40),
    ("nashville", "tn"): (-86.95, 36.05, -86.62, 36.30),
    ("orlando", "fl"): (-81.51, 28.38, -81.20, 28.62),
    ("tampa", "fl"): (-82.64, 27.87, -82.35, 28.10),
    ("san diego", "ca"): (-117.28, 32.64, -117.02, 32.87),
    ("san francisco", "ca"): (-122.52, 37.70, -122.35, 37.83),
}


def resolve_bbox(geo: str) -> tuple[float, float, float, float]:
    token = geo.strip().lower()
    if token in {"us", "usa", "national", "united states"}:
        return US_BBOX
    if "," in token:
        city, state = [part.strip() for part in token.split(",", 1)]
        key = (city, state)
        if key in CITY_BBOX:
            return CITY_BBOX[key]
        raise KeyError(f"Unknown city '{geo}'. Add it to CITY_BBOX or pass a state code.")
    state = token.upper()
    if state in STATE_BBOX:
        return STATE_BBOX[state]
    raise KeyError(f"Unknown geo '{geo}'. Use US, a state code like TX, or city,state like Austin,TX.")


def state_from_geo(geo: str) -> str | None:
    token = geo.strip()
    if token.lower() in {"us", "usa", "national", "united states"}:
        return None
    if "," in token:
        return token.split(",")[-1].strip().upper()
    if len(token.strip()) == 2:
        return token.strip().upper()
    return None


def parquet_stem(geo: str, subsector_key: str | None) -> str:
    geo_part = geo.replace(",", "_").replace(" ", "")
    sector = subsector_key or "all"
    return f"overture_{sector}_{geo_part}"
