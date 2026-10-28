"""Command line interface: ``zaheen --help``."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .export import write as write_export
from .http import HttpClient
from .logging import configure_logging
from .models import BusinessStatus, CampaignSpec, CampaignState, GeographySpec, SourceKind
from .pipeline import STAGES, CampaignRunner
from .registry import get_registry
from .settings import PROJECT_ROOT, get_settings
from .sources import describe_sources
from .sources.base import Source, SourceContext
from .sources.csv_import import CsvImportSource
from .sources.fixture import FixtureSource
from .storage import Database

app = typer.Typer(
    help="Zaheendata: build scored business directories by NAICS sub-sector.", no_args_is_help=True
)
campaign_app = typer.Typer(help="Create, run and export campaigns.", no_args_is_help=True)
app.add_typer(campaign_app, name="campaign")
console = Console()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    s = get_settings()
    configure_logging("DEBUG" if verbose else s.log_level, s.log_json)


@app.command()
def version() -> None:
    console.print(f"zaheendata {__version__}")


@app.command()
def subsectors() -> None:
    """List the configured sub-sectors, NAICS codes and best-fit signals."""
    table = Table(title="Sub-sectors")
    for col in ("key", "name", "NAICS", "scope", "cross-check", "best-fit signal"):
        table.add_column(col)
    for s in get_registry():
        table.add_row(
            s.key, s.name, s.naics, s.scope.value, ", ".join(s.cross_check), s.fit_signal.description
        )
    console.print(table)


@app.command()
def sources() -> None:
    """Show data-source connectors and whether they are configured."""
    table = Table(title="Sources")
    for col in ("name", "kind", "available", "tags", "note"):
        table.add_column(col)
    for s in describe_sources(get_settings()):
        table.add_row(
            s["name"],
            s["kind"],
            "yes" if s["available"] else "no",
            ", ".join(s["tags"]),
            s["reason"] or s["description"],
        )
    console.print(table)


@app.command("db-init")
def db_init() -> None:
    """Create database tables."""

    async def go() -> None:
        db = Database()
        await db.init()
        await db.close()
        console.print(f"database ready: {db.url}")

    _run(go())


def _geo(locations: list[str] | None, state: str | None, country: str, radius_km: float) -> GeographySpec:
    return GeographySpec(locations=locations or [], state=state, country=country, radius_km=radius_km)


@campaign_app.command("create")
def campaign_create(
    subsector: str = typer.Argument(..., help="Sub-sector key, e.g. plumbing_hvac"),
    name: str | None = typer.Option(None, help="Campaign name"),
    location: list[str] | None = typer.Option(None, "--location", "-l", help="City, ST (repeatable)"),
    state: str | None = typer.Option(None, help="Two-letter state for a state-wide search"),
    country: str = typer.Option("US"),
    radius_km: float = typer.Option(25.0),
    long_list: int | None = typer.Option(None, help="Long-list target (default 400)"),
    short_list: int | None = typer.Option(None, help="Short-list target (default 100)"),
    source: list[str] | None = typer.Option(None, "--source", "-s", help="Restrict to these sources"),
) -> None:
    """Create a campaign (does not run it)."""
    sub = get_registry().get(subsector)
    geo = _geo(location, state, country, radius_km)
    spec = CampaignSpec(
        name=name or f"{sub.name} - {geo.describe()}",
        subsector_key=subsector,
        geography=geo,
        long_list_target=long_list,
        short_list_target=short_list,
        sources=source,
    )

    async def go() -> None:
        db = Database()
        await db.init()
        c = await db.create_campaign(spec)
        await db.close()
        console.print(f"created campaign [bold]{c.id}[/bold]: {spec.name}")

    _run(go())


def _progress(stage: str, data: dict[str, Any]) -> None:
    console.print(f"[cyan]{stage}[/cyan] " + " ".join(f"{k}={v}" for k, v in data.items()))


@campaign_app.command("run")
def campaign_run(
    campaign_id: str,
    start_stage: str = typer.Option("collect", help=f"One of {STAGES}"),
    stop_after: str | None = typer.Option(None),
    import_file: list[Path] | None = typer.Option(
        None, "--import", help="Vendor CSV/XLSX exports to merge in"
    ),
) -> None:
    """Run (or resume) a campaign in this process."""

    async def go() -> None:
        db = Database()
        await db.init()
        http = HttpClient()
        ctx = SourceContext(http=http, settings=get_settings())
        extra: list[Source] = [
            CsvImportSource(ctx, p, source_label=f"import:{p.stem}", kind=SourceKind.IMPORT)
            for p in (import_file or [])
        ]
        c = await db.get_campaign(campaign_id)
        if not c:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        runner = CampaignRunner(db, http, extra_sources=extra, progress=_progress)
        result = await runner.run(c, start_stage, stop_after)
        await http.aclose()
        await db.close()
        console.print_json(json.dumps(result.stats, default=str))
        console.print(f"state: [bold]{result.state.value}[/bold]")

    _run(go())


@campaign_app.command("list")
def campaign_list() -> None:
    async def go() -> None:
        db = Database()
        await db.init()
        table = Table(title="Campaigns")
        for col in ("id", "name", "sub-sector", "state", "stage", "selected", "created"):
            table.add_column(col)
        for c in await db.list_campaigns():
            counts = await db.business_counts(c.id)
            table.add_row(
                c.id,
                c.spec.name,
                c.spec.subsector_key,
                c.state.value,
                c.stage or "-",
                str(counts.get("selected", 0)),
                c.created_at.strftime("%Y-%m-%d %H:%M"),
            )
        await db.close()
        console.print(table)

    _run(go())


@campaign_app.command("export")
def campaign_export(
    campaign_id: str,
    output: Path = typer.Argument(..., help="Output path (.csv, .xlsx or .json)"),
    status: str = typer.Option("selected", help="selected | scored | rejected | candidate | all"),
) -> None:
    """Export a campaign's businesses."""

    async def go() -> None:
        db = Database()
        await db.init()
        st = None if status == "all" else BusinessStatus(status)
        items = await db.list_businesses(campaign_id, status=st)
        sup = await db.suppression_sets()
        path = write_export(output, items, sup)
        await db.close()
        console.print(f"wrote {len(items)} rows to {path}")

    _run(go())


@app.command()
def suppress(kind: str, file: Path, reason: str | None = None) -> None:
    """Load a suppression list (one email/phone/domain per line)."""
    values = [line.strip() for line in file.read_text().splitlines() if line.strip()]

    async def go() -> None:
        db = Database()
        await db.init()
        n = await db.add_suppressions(kind, values, reason)
        await db.close()
        console.print(f"added {n} {kind} suppressions")

    _run(go())


@app.command()
def serve(host: str | None = None, port: int | None = None, reload: bool = False) -> None:
    """Start the HTTP API."""
    import uvicorn

    s = get_settings()
    uvicorn.run("zaheendata.api:app", host=host or s.api_host, port=port or s.api_port, reload=reload)


@app.command()
def worker(poll_seconds: float = 5.0, once: bool = False) -> None:
    """Process queued campaigns. Run several workers against Postgres to scale out."""

    async def go() -> None:
        db = Database()
        await db.init()
        http = HttpClient()
        runner = CampaignRunner(db, http, progress=_progress)
        console.print("worker started; waiting for queued campaigns")
        while True:
            c = await db.claim_queued_campaign()
            if c is None:
                if once:
                    break
                await asyncio.sleep(poll_seconds)
                continue
            console.print(f"running campaign {c.id}: {c.spec.name}")
            try:
                await runner.run(c)
            except Exception as exc:  # noqa: BLE001 - keep the worker alive
                console.print(f"[red]campaign {c.id} failed: {exc}[/red]")
        await http.aclose()
        await db.close()

    _run(go())


@app.command()
def demo(
    subsector: str = typer.Option("plumbing_hvac"),
    output: Path = typer.Option(PROJECT_ROOT / "data" / "demo-shortlist.csv"),
    fixture: Path = typer.Option(PROJECT_ROOT / "tests" / "fixtures" / "records.json"),
) -> None:
    """Run the full pipeline offline on bundled fixture data (no API keys needed)."""

    async def go() -> None:
        from .http import ResponseCache
        from .testing import FixtureTransport

        s = get_settings()
        db = Database(url="sqlite+aiosqlite:///./data/demo.db")
        await db.init()
        cache = ResponseCache(s.data_dir / "demo_http_cache.sqlite", ttl_seconds=1)
        http = HttpClient(s, cache=cache, transport=FixtureTransport(fixture.parent))
        ctx = SourceContext(http=http, settings=s)
        spec = CampaignSpec(
            name=f"demo {subsector}",
            subsector_key=subsector,
            geography=GeographySpec(locations=["Austin, TX"]),
            long_list_target=20,
            short_list_target=5,
            sources=[],
            enrich=True,
            contact_enrichment=True,
        )
        c = await db.create_campaign(spec)
        runner = CampaignRunner(db, http, extra_sources=[FixtureSource(ctx, fixture)], progress=_progress)
        result = await runner.run(c)
        items = await db.list_businesses(c.id, status=BusinessStatus.SELECTED)
        write_export(output, items)
        await http.aclose()
        await db.close()
        console.print_json(json.dumps(result.stats, default=str))
        console.print(f"state={result.state.value}; shortlist written to {output}")
        if result.state != CampaignState.COMPLETED:
            raise typer.Exit(1)

    _run(go())


if __name__ == "__main__":
    app()
