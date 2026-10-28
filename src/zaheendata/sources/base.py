from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import ClassVar

from ..http import HttpClient
from ..models import RawRecord, SourceKind, SourceSearchSpec
from ..registry import SubSector
from ..settings import Settings


@dataclass
class SourceContext:
    http: HttpClient
    settings: Settings


class Source(ABC):
    """A connector that yields RawRecords for a search specification.

    ``tags`` link the source to the ``cross_check`` entries of the sub-sector registry so a
    campaign automatically uses the sources the brief recommends for that sub-sector.
    """

    name: ClassVar[str]
    kind: ClassVar[SourceKind]
    tags: ClassVar[set[str]] = set()
    description: ClassVar[str] = ""
    trust: ClassVar[int] = 50  # field precedence during merge (higher wins)
    always_on: ClassVar[bool] = False  # used for every sub-sector when available

    def __init__(self, ctx: SourceContext):
        self.ctx = ctx
        self.http = ctx.http
        self.settings = ctx.settings

    @classmethod
    def available(cls, settings: Settings) -> bool:
        return True

    @classmethod
    def unavailable_reason(cls, settings: Settings) -> str | None:
        return None if cls.available(settings) else "missing configuration"

    def applies_to(self, subsector: SubSector) -> bool:
        return self.always_on or bool(self.tags & set(subsector.cross_check))

    @abstractmethod
    def search(self, spec: SourceSearchSpec) -> AsyncIterator[RawRecord]: ...
