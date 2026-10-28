from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from zaheendata.cache import JsonCache

COLLINFO = "https://index.commoncrawl.org/collinfo.json"
DATA_ROOT = "https://data.commoncrawl.org/"
USER_AGENT = "Zaheendata/0.1 (research; +https://github.com/ssashfaque-creator/Zaheendata)"


def latest_crawl_id(client: httpx.Client | None = None) -> str:
    owned = client is None
    client = client or httpx.Client(timeout=30.0, headers={"User-Agent": USER_AGENT})
    try:
        response = client.get(COLLINFO)
        response.raise_for_status()
        return response.json()[0]["id"]
    finally:
        if owned:
            client.close()


def cdx_lookup(
    domain: str,
    *,
    crawl_id: str,
    cache: JsonCache | None = None,
    client: httpx.Client | None = None,
) -> dict | None:
    cache_key = f"{crawl_id}|{domain}"
    if cache:
        cached = cache.get("cdx", cache_key)
        if cached:
            return cached if cached.get("filename") else None
    owned = client is None
    client = client or httpx.Client(timeout=40.0, headers={"User-Agent": USER_AGENT})
    url = (
        f"https://index.commoncrawl.org/{crawl_id}-index"
        f"?url={domain}/&output=json&filter=status:200&limit=1"
    )
    try:
        response = client.get(url)
        if response.status_code != 200 or not response.text.strip().startswith("{"):
            if cache:
                cache.set("cdx", cache_key, {"filename": ""})
            return None
        record = json.loads(response.text.splitlines()[0])
        if cache:
            cache.set("cdx", cache_key, record)
        return record
    except (httpx.HTTPError, json.JSONDecodeError):
        return None
    finally:
        if owned:
            client.close()


def parse_warc_payload(blob: bytes) -> str:
    try:
        raw = gzip.decompress(blob)
    except OSError:
        raw = blob
    parts = raw.split(b"\r\n\r\n", 2)
    body = parts[-1] if parts else raw
    return body.decode("utf-8", errors="replace")


def fetch_warc_html(
    record: dict,
    *,
    client: httpx.Client | None = None,
    dest: Path | None = None,
) -> str:
    offset = int(record["offset"])
    length = int(record["length"])
    filename = record["filename"]
    owned = client is None
    client = client or httpx.Client(timeout=60.0, headers={"User-Agent": USER_AGENT})
    try:
        response = client.get(
            DATA_ROOT + filename,
            headers={"Range": f"bytes={offset}-{offset + length - 1}"},
        )
        if response.status_code not in {200, 206}:
            return ""
        html = parse_warc_payload(response.content)
        if dest:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(html[:800_000], encoding="utf-8", errors="replace")
        return html
    except httpx.HTTPError:
        return ""
    finally:
        if owned:
            client.close()


def snapshot_domain(
    domain: str,
    *,
    crawl_id: str | None = None,
    cache: JsonCache | None = None,
    html_dir: Path = Path("cache/html"),
    client: httpx.Client | None = None,
) -> dict:
    crawl_id = crawl_id or latest_crawl_id(client)
    record = cdx_lookup(domain, crawl_id=crawl_id, cache=cache, client=client)
    if not record:
        return {"domain": domain, "source": "commoncrawl", "status": 0, "html": ""}
    dest = html_dir / f"{domain}.html"
    html = fetch_warc_html(record, client=client, dest=dest)
    return {
        "domain": domain,
        "source": "commoncrawl",
        "status": 200 if html else 0,
        "html": html,
        "cache_path": str(dest) if html else "",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "url": record.get("url", ""),
    }
