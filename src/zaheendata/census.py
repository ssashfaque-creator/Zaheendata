"""US Census County Business Patterns (CBP) API: free establishment counts by NAICS and size class.

Used to *plan* a campaign before spending any crawl budget: how many 5-19 employee establishments
of this NAICS exist in a city/metro/county/state tells us whether the 300-500 long list is
reachable locally or whether the scope has to widen (the brief's small sub-sectors need state or
national scope). Needs a free API key (api.census.gov/data/key_signup.html).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .http import HttpClient
from .logging import get_logger
from .settings import Settings, get_settings

log = get_logger(__name__)

CBP_YEAR = 2022
CBP_URL = f"https://api.census.gov/data/{CBP_YEAR}/cbp"
NAICS_VAR = "NAICS2017"
# CBP employment-size classes for establishments
EMPSZES_5_9 = "220"
EMPSZES_10_19 = "230"
EMPSZES_ALL = "001"

GEO_LEVELS: dict[str, tuple[str, str | None]] = {
    "state": ("state:{state}", None),
    "county": ("county:*", "state:{state}"),
    "metro": ("metropolitan statistical area/micropolitan statistical area:*", None),
}


@dataclass
class EstablishmentCount:
    geo_name: str
    geo_code: str
    estab_5_9: int = 0
    estab_10_19: int = 0
    estab_all: int = 0

    @property
    def estab_5_19(self) -> int:
        return self.estab_5_9 + self.estab_10_19

    def as_dict(self) -> dict[str, Any]:
        return {
            "geo": self.geo_name,
            "code": self.geo_code,
            "estab_5_9": self.estab_5_9,
            "estab_10_19": self.estab_10_19,
            "estab_5_19": self.estab_5_19,
            "estab_all": self.estab_all,
        }


def parse_cbp_rows(rows: list[list[str]]) -> list[EstablishmentCount]:
    """Census returns a header row then data rows. Fold the size classes into one count per geo."""
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    idx = {h: i for i, h in enumerate(header)}
    geo_names = ("state", "county", "metropolitan statistical area/micropolitan statistical area")
    geo_cols = [h for h in header if h in geo_names]
    out: dict[str, EstablishmentCount] = {}
    for r in body:
        code = "".join(r[idx[c]] for c in geo_cols)
        name = r[idx["NAME"]] if "NAME" in idx else code
        item = out.setdefault(code, EstablishmentCount(geo_name=name, geo_code=code))
        try:
            estab = int(r[idx["ESTAB"]])
        except (KeyError, ValueError, IndexError):
            continue
        size = r[idx["EMPSZES"]] if "EMPSZES" in idx else EMPSZES_ALL
        if size == EMPSZES_5_9:
            item.estab_5_9 += estab
        elif size == EMPSZES_10_19:
            item.estab_10_19 += estab
        elif size == EMPSZES_ALL:
            item.estab_all += estab
    return sorted(out.values(), key=lambda c: -c.estab_5_19)


class CensusCbpClient:
    def __init__(self, http: HttpClient, settings: Settings | None = None):
        self.http = http
        self.settings = settings or get_settings()

    async def establishments(
        self, naics: str, state: str | None, level: str = "state"
    ) -> list[EstablishmentCount]:
        if level not in GEO_LEVELS:
            raise ValueError(f"level must be one of {sorted(GEO_LEVELS)}")
        if level != "metro" and not state:
            raise ValueError(
                "state (FIPS code or two-letter abbreviation) is required for state/county level"
            )
        fips = state_fips(state) if state else ""
        for_clause, in_clause = GEO_LEVELS[level]
        params: dict[str, Any] = {
            "get": "NAME,ESTAB,EMPSZES",
            NAICS_VAR: naics,
            "EMPSZES": ",".join([EMPSZES_ALL, EMPSZES_5_9, EMPSZES_10_19]),
            "for": for_clause.format(state=fips),
        }
        if in_clause:
            params["in"] = in_clause.format(state=fips)
        key = self.settings.secret("census_api_key")
        if not key:
            raise CensusKeyRequired(
                "api.census.gov requires a key: request one free at "
                "https://api.census.gov/data/key_signup.html and set ZAHEEN_CENSUS_API_KEY"
            )
        params["key"] = key
        self.http.set_host_rate("api.census.gov", 2.0, concurrency=2)
        resp = await self.http.get(CBP_URL, params=params, meter="census.cbp")
        if resp.status_code == 204:
            return []  # Census answers 204 when no establishment matches the predicates
        resp.raise_for_status()
        if "missing_key" in str(resp.url) or resp.text.lstrip().startswith("<"):
            raise CensusKeyRequired(
                "api.census.gov rejected the request as unauthenticated; check ZAHEEN_CENSUS_API_KEY"
            )
        return parse_cbp_rows(resp.json())


class CensusKeyRequired(RuntimeError):
    pass


def assess_scope(counts: list[EstablishmentCount], long_list_target: int) -> dict[str, Any]:
    """Rank geographies by 5-19 establishment counts and recommend a scope for the long list."""
    total = sum(c.estab_5_19 for c in counts)
    enough = [c for c in counts if c.estab_5_19 >= long_list_target]
    # Roughly 60-70% of establishments survive the poor-fit filters, so aim for 1.5x the target.
    comfortable = [c for c in counts if c.estab_5_19 >= long_list_target * 1.5]
    if comfortable:
        recommendation = "local"
        note = (
            f"{len(comfortable)} geographies alone hold >= {int(long_list_target * 1.5)} "
            "establishments (5-19 emp)."
        )
    elif enough:
        recommendation = "local_tight"
        note = "Some geographies reach the target but filtering will bite; combine 2-3 adjacent areas."
    elif total >= long_list_target * 1.5:
        recommendation = "state"
        note = "No single geography reaches the target; search the whole state."
    else:
        recommendation = "national"
        note = f"Only {total} establishments (5-19 emp) in scope; widen to several states or national."
    return {
        "total_estab_5_19": total,
        "recommendation": recommendation,
        "note": note,
        "top": [c.as_dict() for c in counts[:15]],
    }


STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08", "CT": "09", "DE": "10",
    "DC": "11", "FL": "12", "GA": "13", "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
    "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34", "NM": "35",
    "NY": "36", "NC": "37", "ND": "38", "OH": "39", "OK": "40", "OR": "41", "PA": "42", "RI": "44",
    "SC": "45", "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56", "PR": "72",
}  # fmt: skip


def state_fips(state: str) -> str:
    s = state.strip().upper()
    if s.isdigit():
        return s.zfill(2)
    try:
        return STATE_FIPS[s]
    except KeyError as exc:
        raise ValueError(f"unknown state {state!r}") from exc
