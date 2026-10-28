"""HTTP API: create and run campaigns, inspect results, export the directory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from . import __version__
from .export import to_csv, to_xlsx
from .http import HttpClient
from .logging import configure_logging, get_logger
from .models import BusinessStatus, CampaignSpec, CampaignState
from .pipeline import STAGES, CampaignRunner
from .registry import get_registry
from .settings import get_settings
from .sources import describe_sources
from .storage import Database

log = get_logger(__name__)


class AppState:
    db: Database
    http: HttpClient
    tasks: set[asyncio.Task[Any]]


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    state.db = Database(settings)
    await state.db.init()
    state.http = HttpClient(settings)
    state.tasks = set()
    yield
    for t in state.tasks:
        t.cancel()
    await state.http.aclose()
    await state.db.close()


app = FastAPI(title="Zaheendata Directory Engine", version=__version__, lifespan=lifespan)


async def require_token(authorization: Annotated[str | None, Header()] = None) -> None:
    token = get_settings().secret("api_token")
    if token and authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


Auth = Depends(require_token)


class RunRequest(BaseModel):
    start_stage: str = "collect"
    stop_after: str | None = None
    background: bool = True


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/subsectors", dependencies=[Auth])
async def subsectors() -> list[dict[str, Any]]:
    return [s.model_dump() for s in get_registry()]


@app.get("/sources", dependencies=[Auth])
async def sources() -> list[dict[str, Any]]:
    return describe_sources(get_settings())


@app.post("/campaigns", dependencies=[Auth], status_code=201)
async def create_campaign(spec: CampaignSpec) -> dict[str, Any]:
    try:
        get_registry().get(spec.subsector_key)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    campaign = await state.db.create_campaign(spec)
    return campaign.model_dump(mode="json")


@app.get("/campaigns", dependencies=[Auth])
async def list_campaigns() -> list[dict[str, Any]]:
    return [c.model_dump(mode="json") for c in await state.db.list_campaigns()]


@app.get("/campaigns/{campaign_id}", dependencies=[Auth])
async def get_campaign(campaign_id: str) -> dict[str, Any]:
    c = await state.db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    data = c.model_dump(mode="json")
    data["counts"] = await state.db.business_counts(campaign_id)
    return data


@app.post("/campaigns/{campaign_id}/run", dependencies=[Auth], status_code=202)
async def run_campaign(campaign_id: str, req: RunRequest | None = None) -> dict[str, Any]:
    req = req or RunRequest()
    c = await state.db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    if c.state == CampaignState.RUNNING:
        raise HTTPException(status_code=409, detail="campaign already running")
    if req.start_stage not in STAGES:
        raise HTTPException(status_code=422, detail=f"start_stage must be one of {STAGES}")
    runner = CampaignRunner(state.db, state.http)
    if req.background:
        task = asyncio.create_task(runner.run(c, req.start_stage, req.stop_after))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)
        return {"campaign_id": campaign_id, "state": "running", "start_stage": req.start_stage}
    result = await runner.run(c, req.start_stage, req.stop_after)
    return result.model_dump(mode="json")


@app.post("/campaigns/{campaign_id}/queue", dependencies=[Auth], status_code=202)
async def queue_campaign(campaign_id: str) -> dict[str, str]:
    """Hand the campaign to the worker pool (``zaheen worker``) instead of running in-process."""
    c = await state.db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    await state.db.update_campaign(campaign_id, state=CampaignState.QUEUED)
    return {"campaign_id": campaign_id, "state": "queued"}


@app.get("/campaigns/{campaign_id}/businesses", dependencies=[Auth])
async def list_businesses(
    campaign_id: str,
    status: BusinessStatus | None = None,
    limit: int = Query(100, le=1000),
    offset: int = 0,
) -> list[dict[str, Any]]:
    items = await state.db.list_businesses(campaign_id, status=status, limit=limit, offset=offset)
    return [b.model_dump(mode="json") for b in items]


@app.get("/campaigns/{campaign_id}/export", dependencies=[Auth])
async def export_campaign(
    campaign_id: str, fmt: str = "csv", status: BusinessStatus | None = BusinessStatus.SELECTED
) -> Response:
    c = await state.db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="campaign not found")
    items = await state.db.list_businesses(campaign_id, status=status)
    suppressions = await state.db.suppression_sets()
    filename = f"{c.spec.subsector_key}-{c.spec.geography.describe().replace(' ', '_').replace(',', '')}"
    if fmt == "xlsx":
        return Response(
            to_xlsx(items, suppressions),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
        )
    if fmt == "json":
        return JSONResponse([b.model_dump(mode="json") for b in items])
    return PlainTextResponse(
        to_csv(items, suppressions),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}.csv"'},
    )


class SuppressionRequest(BaseModel):
    kind: str  # email | phone | domain
    values: list[str]
    reason: str | None = None


@app.post("/suppressions", dependencies=[Auth], status_code=201)
async def add_suppressions(req: SuppressionRequest) -> dict[str, int]:
    if req.kind not in ("email", "phone", "domain"):
        raise HTTPException(status_code=422, detail="kind must be email, phone or domain")
    return {"added": await state.db.add_suppressions(req.kind, req.values, req.reason)}
