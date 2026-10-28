from __future__ import annotations

import re

from zaheendata.normalize import normalize_name

# Neighborly / Clockwork / other national home-service brands that fail
# the guide's "drop franchises and chains" rule.
FRANCHISE_BRANDS = (
    "roto rooter",
    "roto-rooter",
    "mr rooter",
    "mister rooter",
    "rooter man",
    "rooterman",
    "aire serv",
    "aireserv",
    "benjamin franklin plumbing",
    "one hour heating",
    "mr electric",
    "mister electric",
    "mister sparky",
    "mr sparkle",
    "service experts",
    "american residential services",
    "ars rescue rooter",
    "rescue rooter",
    "clockwork home services",
    "neighborly",
    "groundworks",
    "waterproofer",
    "conserva irrigation",
    "mosquito joe",
    "mosquito squad",
    "orkin",
    "terminix",
    "rentokil",
    "aptive",
    "trutech",
    "truly nolen",
    "jiffy lube",
    "the groutsmith",
    "merry maids",
    "molly maid",
    "the cleaning authority",
    "servicemaster",
    "stanley steemer",
    "rainbow international",
    "servpro",
    "paul davis",
    "carpet court",
    "great clips",
    "sport clips",
    "supercuts",
    "fantastic sams",
    "cost cutters",
    "hand and stone",
    "european wax",
    "massage envy",
    "the ups store",
    "1 800 got junk",
    "college hunks",
    "two men and a truck",
    "budget blinds",
    "floor coverings international",
    "window genie",
    "fish window cleaning",
    "pinot's palette",
    "edible arrangements",
    "1 800 flowers",
    "teleflora",
    "ftd",
    "petco",
    "petsmart",
    "pet supplies plus",
    "dick's sporting goods",
    "academy sports",
    "hibbett",
    "foot locker",
    "scheels",
    "bass pro",
    "cabela's",
    "johnson controls",
    "trane technologies",
    "carrier enterprise",
    "lennox national",
    "baker distributing",
    "johnstone supply",
    "ferguson enterprises",
    "home depot",
    "lowe's",
    "lowes",
)

_FRANCHISE_RE = re.compile(
    r"\b(" + "|".join(re.escape(brand) for brand in sorted(FRANCHISE_BRANDS, key=len, reverse=True)) + r")\b"
)


def franchise_hit(name: str | None) -> str:
    haystack = normalize_name(name)
    match = _FRANCHISE_RE.search(haystack)
    return match.group(1) if match else ""
