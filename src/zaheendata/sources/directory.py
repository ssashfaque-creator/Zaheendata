"""Config-driven HTML directory scraper (``config/directories.yaml``)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from functools import lru_cache
from typing import Any, ClassVar
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from ..geo import US_STATES
from ..http import RobotsDisallowed
from ..logging import get_logger
from ..models import Address, RawRecord, SourceKind, SourceSearchSpec
from ..registry import SubSector, get_registry
from ..settings import get_settings
from .base import Source

log = get_logger(__name__)


class FieldSelector(BaseModel):
    selector: str
    attr: str | None = None


class DirectoryDef(BaseModel):
    key: str
    name: str
    tags: list[str]
    enabled: bool = False
    list_url: str
    max_pages: int = 3
    item: str
    fields: dict[str, FieldSelector] = Field(default_factory=dict)


@lru_cache
def load_directories() -> list[DirectoryDef]:
    path = get_settings().config_dir / "directories.yaml"
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [DirectoryDef(**d) for d in data.get("directories", [])]


class DirectorySource(Source):
    name = "directory"
    kind = SourceKind.DIRECTORY
    tags: ClassVar[set[str]] = {
        "issa_directory",
        "nsga_directory",
        "astra_directory",
        "saf_directory",
        "ftd_directory",
        "teleflora_directory",
        "thumbtack",
        "houzz",
        "angi",
        "vagaro",
        "fresha",
        "booksy",
        "faire",
        "downtown_associations",
        "game_publisher_store_locators",
    }
    description = "Config-driven scraper for public member directories / store locators (robots.txt aware)."
    trust = 45

    def applies_to(self, subsector: SubSector) -> bool:
        return any(d.enabled and set(d.tags) & set(subsector.cross_check) for d in load_directories())

    @staticmethod
    def _extract(card: Any, sel: FieldSelector, base_url: str) -> str | None:
        node = card.select_one(sel.selector)
        if node is None:
            return None
        if sel.attr:
            value = node.get(sel.attr)
            if isinstance(value, list):
                value = value[0] if value else None
            if value and sel.attr == "href":
                value = urljoin(base_url, value)
            return value.strip() if isinstance(value, str) else None
        return " ".join(node.get_text(" ", strip=True).split()) or None

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        subsector = get_registry().get(spec.subsector_key)
        city = (spec.location_text or "").split(",")[0].strip() if spec.location_text else ""
        state = spec.region or ""
        state_name = US_STATES.get(state, ("",))[0] if state else ""
        for d in load_directories():
            if not d.enabled or not (set(d.tags) & set(subsector.cross_check)):
                continue
            for query in spec.queries[:2]:
                for page in range(1, d.max_pages + 1):
                    url = d.list_url.format(
                        query=query.replace(" ", "-"),
                        city=city.lower().replace(" ", "-"),
                        state=state.lower(),
                        state_name=state_name,
                        zip="",
                        page=page,
                    )
                    try:
                        resp = await self.http.get(
                            url, check_robots=True, max_bytes=self.settings.website_max_bytes
                        )
                    except RobotsDisallowed:
                        log.warning("directory.robots_disallowed", directory=d.key, url=url)
                        break
                    except Exception as exc:  # noqa: BLE001
                        log.warning("directory.fetch_failed", directory=d.key, url=url, error=str(exc))
                        break
                    if resp.status_code != 200:
                        break
                    soup = BeautifulSoup(resp.text, "lxml")
                    cards = soup.select(d.item)
                    if not cards:
                        break
                    for card in cards:
                        values = {f: self._extract(card, sel, url) for f, sel in d.fields.items()}
                        if not values.get("name"):
                            continue
                        rc = values.get("review_count")
                        yield RawRecord(
                            source=f"directory:{d.key}",
                            source_kind=self.kind,
                            source_id=values.get("url"),
                            url=values.get("url"),
                            name=str(values["name"]),
                            phone=values.get("phone"),
                            website=values.get("website"),
                            address=Address(
                                line1=values.get("line1"),
                                city=values.get("city") or (city or None),
                                region=values.get("region") or (state or None),
                                postal_code=values.get("postal_code"),
                            ),
                            review_count=int(re.sub(r"\D", "", rc)) if rc and re.search(r"\d", rc) else None,
                            attributes={"directory": d.name, "query": query},
                        )
