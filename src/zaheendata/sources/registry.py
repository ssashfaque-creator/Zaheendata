from __future__ import annotations

from typing import Any

from ..registry import SubSector
from ..settings import Settings
from .base import Source, SourceContext
from .directory import DirectorySource
from .google_maps_scraper import GoogleMapsScraperSource
from .google_places import GooglePlacesSource
from .overpass import OverpassSource
from .ppp_loans import PppLoansSource
from .public_datasets import PublicDatasetSource
from .sam_gov import SamGovSource
from .yelp import YelpSource

SOURCE_CLASSES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (
        GooglePlacesSource,
        GoogleMapsScraperSource,
        YelpSource,
        OverpassSource,
        SamGovSource,
        PppLoansSource,
        PublicDatasetSource,
        DirectorySource,
    )
}


def describe_sources(settings: Settings) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "kind": cls.kind.value,
            "available": cls.available(settings),
            "reason": cls.unavailable_reason(settings),
            "tags": sorted(cls.tags),
            "description": cls.description,
        }
        for name, cls in SOURCE_CLASSES.items()
    ]


def build_sources(
    ctx: SourceContext,
    subsector: SubSector,
    only: list[str] | None = None,
    extra: list[Source] | None = None,
) -> list[Source]:
    """Instantiate the sources that are available and relevant for a sub-sector."""
    chosen: list[Source] = []
    for name, cls in SOURCE_CLASSES.items():
        if only is not None and name not in only:
            continue
        if not cls.available(ctx.settings):
            continue
        instance = cls(ctx)
        if only is not None or instance.applies_to(subsector):
            chosen.append(instance)
    chosen.extend(extra or [])
    return chosen
