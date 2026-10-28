"""Loads the sub-sector registry (``config/subsectors.yaml``)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .models import GeoScope
from .settings import get_settings


class FitSignalRule(BaseModel):
    description: str
    features: list[str]
    negative_features: list[str] = Field(default_factory=list)
    strong_threshold: int = 2


class SubSector(BaseModel):
    key: str
    name: str
    naics: str
    scope: GeoScope = GeoScope.METRO
    cross_check: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    yelp_categories: list[str] = Field(default_factory=list)
    osm_tags: list[str] = Field(default_factory=list)
    apollo_keywords: list[str] = Field(default_factory=list)
    fit_signal: FitSignalRule


class SubSectorRegistry(BaseModel):
    version: int = 1
    subsectors: dict[str, SubSector]

    def get(self, key: str) -> SubSector:
        try:
            return self.subsectors[key]
        except KeyError:
            raise KeyError(
                f"Unknown sub-sector '{key}'. Known: {', '.join(sorted(self.subsectors))}"
            ) from None

    def by_naics(self, naics: str) -> SubSector | None:
        return next((s for s in self.subsectors.values() if s.naics == naics), None)

    def __iter__(self):
        return iter(self.subsectors.values())

    def __len__(self) -> int:
        return len(self.subsectors)


def load_registry(path: Path | None = None) -> SubSectorRegistry:
    path = path or get_settings().config_dir / "subsectors.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    subsectors = {k: SubSector(key=k, **v) for k, v in data["subsectors"].items()}
    return SubSectorRegistry(version=data.get("version", 1), subsectors=subsectors)


@lru_cache
def get_registry() -> SubSectorRegistry:
    return load_registry()
