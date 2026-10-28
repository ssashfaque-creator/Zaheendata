"""Browser-based Google Maps scraper (Playwright). EXPERIMENTAL and disabled by default.

Google's Terms of Service prohibit scraping Maps; use the Places API connector for compliant
production runs. This connector exists for organisations that have made their own legal
assessment (e.g. research use) and want a fallback when API budget is exhausted. It renders the
search feed, scrolls until the feed stops growing, and parses listing cards.

Enable with ``ZAHEEN_ENABLE_GOOGLE_MAPS_SCRAPER=true`` and ``pip install 'zaheendata[browser]'``
followed by ``playwright install chromium``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar
from urllib.parse import quote_plus

from ..logging import get_logger
from ..models import Address, GeoPoint, RawRecord, SourceKind, SourceSearchSpec
from ..settings import Settings
from .base import Source

log = get_logger(__name__)

_COORD_RE = re.compile(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)")
_REVIEWS_RE = re.compile(r"\(([\d,]+)\)")


class GoogleMapsScraperSource(Source):
    name = "google_maps_scraper"
    kind = SourceKind.MAPS
    tags: ClassVar[set[str]] = {"google_maps_scraper"}
    description = "Playwright scraper for Google Maps search results (experimental, off by default)."
    trust = 50
    always_on = True

    @classmethod
    def available(cls, settings: Settings) -> bool:
        if not settings.enable_google_maps_scraper:
            return False
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        if not settings.enable_google_maps_scraper:
            return "disabled (ZAHEEN_ENABLE_GOOGLE_MAPS_SCRAPER=false); prefer google_places"
        return (
            None
            if cls.available(settings)
            else "pip install 'zaheendata[browser]' && playwright install chromium"
        )

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(locale="en-US", user_agent=self.settings.user_agent)
            page = await context.new_page()
            seen: set[str] = set()
            try:
                for query in spec.queries:
                    q = f"{query} {spec.location_text or ''}".strip()
                    url = f"https://www.google.com/maps/search/{quote_plus(q)}"
                    if spec.center:
                        url += f"/@{spec.center.lat},{spec.center.lng},12z"
                    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    await self._dismiss_consent(page)
                    try:
                        await page.wait_for_selector("div[role='feed']", timeout=15_000)
                    except Exception:  # noqa: BLE001
                        continue
                    await self._scroll_feed(page, spec.limit)
                    for card in await page.query_selector_all("div[role='feed'] > div > div[jsaction]"):
                        rec = await self._parse_card(card, spec)
                        if rec and rec.source_id not in seen:
                            seen.add(rec.source_id or rec.name)
                            yield rec
                    await asyncio.sleep(2)
            finally:
                await browser.close()

    async def _dismiss_consent(self, page: Any) -> None:
        for text in ("Accept all", "I agree", "Reject all"):
            try:
                await page.get_by_role("button", name=text).click(timeout=2_000)
                return
            except Exception:  # noqa: BLE001
                continue

    async def _scroll_feed(self, page: Any, limit: int) -> None:
        feed = await page.query_selector("div[role='feed']")
        if not feed:
            return
        stable = 0
        last = 0
        while stable < 4:
            await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            await asyncio.sleep(1.5)
            count = len(await page.query_selector_all("div[role='feed'] a[href*='/maps/place/']"))
            if count >= limit:
                return
            stable = stable + 1 if count == last else 0
            last = count

    async def _parse_card(self, card: Any, spec: SourceSearchSpec) -> RawRecord | None:
        link = await card.query_selector("a[href*='/maps/place/']")
        if link is None:
            return None
        href = await link.get_attribute("href") or ""
        name = await link.get_attribute("aria-label") or ""
        if not name:
            return None
        text = " ".join((await card.inner_text()).split())
        m = _COORD_RE.search(href)
        rating_m = re.search(r"(\d\.\d)\s*\(", text)
        reviews_m = _REVIEWS_RE.search(text)
        phone_m = re.search(r"\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}", text)
        website_el = await card.query_selector("a[data-value='Website']")
        return RawRecord(
            source=self.name,
            source_kind=self.kind,
            source_id=href.split("?")[0],
            url=href,
            name=name,
            phone=phone_m.group(0) if phone_m else None,
            website=await website_el.get_attribute("href") if website_el else None,
            address=Address(region=spec.region, formatted=None),
            location=GeoPoint(lat=float(m.group(1)), lng=float(m.group(2))) if m else None,
            rating=float(rating_m.group(1)) if rating_m else None,
            review_count=int(reviews_m.group(1).replace(",", "")) if reviews_m else None,
            attributes={"card_text": text[:400]},
        )
