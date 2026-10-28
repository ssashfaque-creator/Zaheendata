"""Offline HTTP transport used by ``zaheen demo`` and the test-suite.

Serves ``<root>/sites/<host>/<path>.html`` for website crawls, answers the geocoder with a fixed
point, and returns 404 for everything else, so the full pipeline runs without network access."""

from __future__ import annotations

import json
from pathlib import Path

import httpx


class FixtureTransport(httpx.AsyncBaseTransport):
    def __init__(self, root: Path, geocode: tuple[float, float] = (30.2672, -97.7431)):
        self.root = Path(root)
        self.geocode = geocode
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        if "nominatim" in host:
            body = json.dumps(
                [
                    {
                        "lat": str(self.geocode[0]),
                        "lon": str(self.geocode[1]),
                        "address": {"county": "Travis County"},
                    }
                ]
            )
            return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        if "photon" in host:
            return httpx.Response(
                200, content=b'{"features":[]}', headers={"content-type": "application/json"}
            )
        if "geocoding.geo.census.gov" in host:
            if "addressbatch" in path:
                return httpx.Response(200, content=b"", headers={"content-type": "text/csv"})
            body = json.dumps({"result": {"addressMatches": []}})
            return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        if "overpass" in host:
            return httpx.Response(
                200, content=b'{"elements":[]}', headers={"content-type": "application/json"}
            )
        if "duckduckgo.com" in host:
            if host.startswith("api."):
                return httpx.Response(
                    200, content=b'{"AbstractURL":"","RelatedTopics":[]}',
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
        if host in {"www.bing.com", "bing.com"}:
            return httpx.Response(200, content=b"<html></html>", headers={"content-type": "text/html"})
        if "maps.googleapis.com" in host and path.endswith("/geocode/json"):
            body = json.dumps(
                {"results": [{"geometry": {"location": {"lat": self.geocode[0], "lng": self.geocode[1]}}}]}
            )
            return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})
        if path == "/robots.txt":
            return httpx.Response(404)
        host_dir = self.root / "sites" / (host[4:] if host.startswith("www.") else host)
        rel = path.strip("/") or "index"
        for candidate in (host_dir / f"{rel}.html", host_dir / rel / "index.html", host_dir / rel):
            if candidate.is_file():
                return httpx.Response(
                    200, content=candidate.read_bytes(), headers={"content-type": "text/html; charset=utf-8"}
                )
        return httpx.Response(404, content=b"not found", headers={"content-type": "text/html"})
