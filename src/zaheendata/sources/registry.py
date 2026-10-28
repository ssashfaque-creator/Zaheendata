from __future__ import annotations

from typing import Any

from ..registry import SubSector
from ..settings import Settings
from .apollo import ApolloSource
from .base import Source, SourceContext
from .data_axle import DataAxleSource
from .directory import DirectorySource
from .google_maps_scraper import GoogleMapsScraperSource
from .google_places import GooglePlacesSource
from .license import StateLicenseSource
from .overpass import OverpassSource
from .yelp import YelpSource

SOURCE_CLASSES: dict[str, type[Source]] = {
    cls.name: cls
    for cls in (
        GooglePlacesSource,
        GoogleMapsScraperSource,
        YelpSource,
        OverpassSource,
        ApolloSource,
        DataAxleSource,
        StateLicenseSource,
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
