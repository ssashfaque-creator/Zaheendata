"""Local Wappalyzer-style technology detection plus an optional BuiltWith API lookup."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ..http import HttpClient
from ..logging import get_logger
from ..models import Technology, TechStack
from ..settings import get_settings

log = get_logger(__name__)


@dataclass
class Fingerprint:
    name: str
    category: str
    html: list[re.Pattern[str]]
    scripts: list[re.Pattern[str]]
    headers: list[re.Pattern[str]]
    domains: list[str]


@lru_cache
def load_fingerprints(path: Path | None = None) -> list[Fingerprint]:
    path = path or get_settings().config_dir / "tech_fingerprints.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    fps: list[Fingerprint] = []
    for t in data.get("technologies", []):
        fps.append(
            Fingerprint(
                name=t["name"],
                category=t["category"],
                html=[re.compile(p, re.IGNORECASE) for p in t.get("html", [])],
                scripts=[re.compile(p, re.IGNORECASE) for p in t.get("scripts", [])],
                headers=[re.compile(p, re.IGNORECASE) for p in t.get("headers", [])],
                domains=[d.lower() for d in t.get("domains", [])],
            )
        )
    return fps


def _host(url: str) -> str:
    host = urlsplit(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


class TechStackDetector:
    def __init__(self, fingerprints: list[Fingerprint] | None = None):
        self.fingerprints = fingerprints or load_fingerprints()

    def detect(self, pages: list[Any], own_domain: str | None = None) -> TechStack:
        """``pages`` are CrawledPage objects (html, headers, script_urls, link_urls)."""
        found: dict[str, Technology] = {}
        html_blob = "\n".join(p.html for p in pages)
        script_urls = [u for p in pages for u in p.script_urls]
        header_lines = "\n".join(f"{k}: {v}" for p in pages for k, v in p.headers.items())
        external_hosts = {
            _host(u)
            for p in pages
            for u in (p.link_urls + p.script_urls + p.iframe_urls)
            if u.startswith("http")
        }
        if own_domain:
            external_hosts = {
                h for h in external_hosts if h != own_domain and not h.endswith("." + own_domain)
            }
        for fp in self.fingerprints:
            hit = False
            if (
                any(p.search(html_blob) for p in fp.html)
                or any(p.search(u) for p in fp.scripts for u in script_urls)
                or any(p.search(header_lines) for p in fp.headers)
                or any(h == d or h.endswith("." + d) for d in fp.domains for h in external_hosts)
            ):
                hit = True
            if hit and fp.name not in found:
                found[fp.name] = Technology(name=fp.name, category=fp.category)
        return TechStack(technologies=list(found.values()))


class BuiltWithClient:
    """BuiltWith Domain API (https://api.builtwith.com/v21/api.json)."""

    URL = "https://api.builtwith.com/v21/api.json"

    def __init__(self, http: HttpClient):
        self.http = http
        self.key = get_settings().secret("builtwith_api_key")

    @property
    def available(self) -> bool:
        return bool(self.key)

    async def lookup(self, domain: str) -> list[Technology]:
        if not self.key:
            return []
        try:
            data = await self.http.get_json(
                self.URL,
                params={"KEY": self.key, "LOOKUP": domain, "HIDETEXT": "yes"},
                meter="builtwith.lookup",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("builtwith.failed", domain=domain, error=str(exc))
            return []
        techs: list[Technology] = []
        for result in data.get("Results", []):
            for path in result.get("Result", {}).get("Paths", []):
                for t in path.get("Technologies", []):
                    cats = t.get("Categories") or ["other"]
                    techs.append(
                        Technology(
                            name=t.get("Name", "?"),
                            category=str(cats[0]).lower().replace(" ", "_"),
                            source="builtwith",
                        )
                    )
        return techs
