"""Fixture source: replays RawRecords from a JSON file. Used by tests and ``zaheen demo`` so the
whole pipeline can be exercised offline without API keys."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

from ..models import RawRecord, SourceKind, SourceSearchSpec
from .base import Source


class FixtureSource(Source):
    name = "fixture"
    kind = SourceKind.MAPS
    tags: ClassVar[set[str]] = {"fixture"}
    description = "Replays records from a JSON fixture (offline demos and tests)."
    trust = 50
    always_on = True

    def __init__(self, ctx, path: Path):
        super().__init__(ctx)
        self.path = Path(path)

    async def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        for item in data:
            if item.get("subsector_key") not in (None, spec.subsector_key):
                continue
            item = {k: v for k, v in item.items() if k != "subsector_key"}
            item.setdefault("source", "fixture")
            item.setdefault("source_kind", SourceKind.MAPS)
            for review in item.get("reviews", []):
                # fixtures express recency relatively so they never go stale
                if "days_ago" in review:
                    review["published_at"] = (now - timedelta(days=review.pop("days_ago"))).isoformat()
            yield RawRecord(**item)
