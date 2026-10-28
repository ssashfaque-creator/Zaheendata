"""Persistence: campaigns, raw source records, resolved businesses, suppression list.

SQLite (aiosqlite) for local use; Postgres (asyncpg) for production via ``ZAHEEN_DATABASE_URL``.
Full Pydantic payloads are stored as JSON alongside indexed scalar columns used for queries."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import Business, BusinessStatus, Campaign, CampaignSpec, CampaignState, RawRecord, utcnow
from .settings import Settings, get_settings


class Base(DeclarativeBase):
    pass


class CampaignRow(Base):
    __tablename__ = "campaigns"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    subsector_key: Mapped[str] = mapped_column(String(64), index=True)
    spec: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), index=True, default=CampaignState.CREATED.value)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RawRecordRow(Base):
    __tablename__ = "raw_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    source_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str] = mapped_column(String(300))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_raw_campaign_source_sid", "campaign_id", "source", "source_id"),)


class BusinessRow(Base):
    __tablename__ = "businesses"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    phone_e164: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    website: Mapped[str | None] = mapped_column(String(500), nullable=True)
    google_place_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    city: Mapped[str | None] = mapped_column(String(120), nullable=True)
    region: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    postal_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    review_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rating: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True, default=BusinessStatus.CANDIDATE.value)
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SuppressionRow(Base):
    __tablename__ = "suppressions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)  # email | phone | domain
    value: Mapped[str] = mapped_column(String(255), index=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_suppression_kind_value", "kind", "value", unique=True),)


def _dump(model: Any) -> dict[str, Any]:
    return json.loads(model.model_dump_json())


class Database:
    def __init__(self, settings: Settings | None = None, url: str | None = None):
        self.settings = settings or get_settings()
        self.url = url or self.settings.database_url
        if self.url.startswith("sqlite"):
            path = self.url.split("///")[-1]
            if path and path != ":memory:":
                from pathlib import Path

                Path(path).parent.mkdir(parents=True, exist_ok=True)
        engine_kw: dict[str, Any] = {"future": True, "pool_pre_ping": not self.url.startswith("sqlite")}
        if self.url.startswith("sqlite"):
            engine_kw["connect_args"] = {"timeout": 30.0}
        self.engine = create_async_engine(self.url, **engine_kw)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if self.url.startswith("sqlite"):
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                await conn.execute(text("PRAGMA busy_timeout=30000"))

    async def close(self) -> None:
        await self.engine.dispose()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.sessions() as s:
            yield s

    # ------------------------------------------------------------------ campaigns
    async def create_campaign(self, spec: CampaignSpec) -> Campaign:
        campaign = Campaign(spec=spec)
        async with self.session() as s:
            s.add(
                CampaignRow(
                    id=campaign.id,
                    name=spec.name,
                    subsector_key=spec.subsector_key,
                    spec=_dump(spec),
                    state=campaign.state.value,
                    stats={},
                )
            )
            await s.commit()
        return campaign

    @staticmethod
    def _campaign_from_row(row: CampaignRow) -> Campaign:
        return Campaign(
            id=row.id,
            spec=CampaignSpec(**row.spec),
            state=CampaignState(row.state),
            stage=row.stage,
            stats=row.stats or {},
            error=row.error,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self.session() as s:
            row = await s.get(CampaignRow, campaign_id)
            return self._campaign_from_row(row) if row else None

    async def list_campaigns(self, limit: int = 100) -> list[Campaign]:
        async with self.session() as s:
            rows = (
                await s.execute(select(CampaignRow).order_by(CampaignRow.created_at.desc()).limit(limit))
            ).scalars()
            return [self._campaign_from_row(r) for r in rows]

    async def update_campaign(self, campaign_id: str, **fields: Any) -> None:
        if "state" in fields and isinstance(fields["state"], CampaignState):
            fields["state"] = fields["state"].value
        fields["updated_at"] = utcnow()
        async with self.session() as s:
            await s.execute(update(CampaignRow).where(CampaignRow.id == campaign_id).values(**fields))
            await s.commit()

    async def claim_queued_campaign(self) -> Campaign | None:
        """Atomically move one QUEUED campaign to RUNNING (worker loop)."""
        async with self.session() as s:
            stmt = (
                select(CampaignRow)
                .where(CampaignRow.state == CampaignState.QUEUED.value)
                .order_by(CampaignRow.created_at)
            )
            if not self.url.startswith("sqlite"):
                stmt = stmt.with_for_update(skip_locked=True)
            row = (await s.execute(stmt.limit(1))).scalar_one_or_none()
            if row is None:
                return None
            row.state = CampaignState.RUNNING.value
            row.updated_at = utcnow()
            await s.commit()
            return self._campaign_from_row(row)

    # ------------------------------------------------------------------ raw records
    async def add_raw_records(self, campaign_id: str, records: Iterable[RawRecord]) -> int:
        rows = [
            RawRecordRow(
                campaign_id=campaign_id,
                source=r.source,
                source_id=r.source_id,
                name=r.name[:300],
                payload=_dump(r),
                fetched_at=r.fetched_at,
            )
            for r in records
        ]
        if not rows:
            return 0
        async with self.session() as s:
            s.add_all(rows)
            await s.commit()
        return len(rows)

    async def list_raw_records(self, campaign_id: str) -> list[RawRecord]:
        async with self.session() as s:
            rows = (
                await s.execute(select(RawRecordRow.payload).where(RawRecordRow.campaign_id == campaign_id))
            ).scalars()
            return [RawRecord(**p) for p in rows]

    async def clear_raw_records(self, campaign_id: str) -> None:
        async with self.session() as s:
            await s.execute(delete(RawRecordRow).where(RawRecordRow.campaign_id == campaign_id))
            await s.commit()

    # ------------------------------------------------------------------ businesses
    @staticmethod
    def _business_row(b: Business) -> BusinessRow:
        return BusinessRow(
            id=b.id,
            campaign_id=b.campaign_id or "",
            name=b.name[:300],
            normalized_name=b.normalized_name[:300],
            phone_e164=b.phone_e164,
            domain=b.domain,
            website=b.website,
            google_place_id=b.google_place_id,
            city=b.address.city if b.address else None,
            region=b.address.region if b.address else None,
            postal_code=b.address.postal_code if b.address else None,
            review_count=b.review_count,
            rating=b.rating,
            status=b.status.value,
            rejection_reason=(b.rejection_reason or None) and b.rejection_reason[:300],
            score_total=b.score.total if b.score else None,
            rank=b.score.rank if b.score else None,
            payload=_dump(b),
            created_at=b.created_at,
            updated_at=utcnow(),
        )

    async def replace_businesses(self, campaign_id: str, businesses: Iterable[Business]) -> int:
        rows = [self._business_row(b) for b in businesses]
        async with self.session() as s:
            await s.execute(delete(BusinessRow).where(BusinessRow.campaign_id == campaign_id))
            s.add_all(rows)
            await s.commit()
        return len(rows)

    async def upsert_businesses(self, businesses: Iterable[Business]) -> int:
        n = 0
        async with self.session() as s:
            for b in businesses:
                await s.merge(self._business_row(b))
                n += 1
            await s.commit()
        return n

    async def list_businesses(
        self,
        campaign_id: str,
        status: BusinessStatus | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[Business]:
        async with self.session() as s:
            stmt = select(BusinessRow.payload).where(BusinessRow.campaign_id == campaign_id)
            if status:
                stmt = stmt.where(BusinessRow.status == status.value)
            stmt = stmt.order_by(
                BusinessRow.rank.is_(None), BusinessRow.rank, BusinessRow.review_count.desc()
            ).offset(offset)
            if limit:
                stmt = stmt.limit(limit)
            return [Business(**p) for p in (await s.execute(stmt)).scalars()]

    async def business_counts(self, campaign_id: str) -> dict[str, int]:
        async with self.session() as s:
            rows = await s.execute(
                select(BusinessRow.status, func.count())
                .where(BusinessRow.campaign_id == campaign_id)
                .group_by(BusinessRow.status)
            )
            return {status: count for status, count in rows.all()}

    # ------------------------------------------------------------------ suppressions
    async def add_suppressions(self, kind: str, values: Iterable[str], reason: str | None = None) -> int:
        n = 0
        async with self.session() as s:
            for v in values:
                v = v.strip().lower()
                if not v:
                    continue
                exists = await s.execute(
                    select(SuppressionRow.id).where(SuppressionRow.kind == kind, SuppressionRow.value == v)
                )
                if exists.scalar_one_or_none() is None:
                    s.add(SuppressionRow(kind=kind, value=v, reason=reason))
                    n += 1
            await s.commit()
        return n

    async def suppression_sets(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {"email": set(), "phone": set(), "domain": set()}
        async with self.session() as s:
            for kind, value in (await s.execute(select(SuppressionRow.kind, SuppressionRow.value))).all():
                out.setdefault(kind, set()).add(value)
        return out
