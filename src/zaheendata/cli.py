"""Command line interface: ``zaheen --help``."""

from __future__ import annotations

import asyncio
import json
import sys
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
datasets_app = typer.Typer(help="Inspect and download government / public datasets.", no_args_is_help=True)
app.add_typer(datasets_app, name="datasets")
console = Console(force_terminal=True)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _flush() -> None:
    try:
        sys.stdout.reconfigure(line_buffering=True)
        sys.stderr.reconfigure(line_buffering=True)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    _flush()
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


@app.command()
def plan(
    subsector: str = typer.Argument(..., help="Sub-sector key, e.g. florists"),
    state: str | None = typer.Option(None, help="Two-letter state; omit with --level metro"),
    level: str = typer.Option("county", help="county | state | metro"),
) -> None:
    """Size the geography with Census CBP counts of 5-19 employee establishments (free key)."""
    from .census import CensusCbpClient, CensusKeyRequired, assess_scope

    sub = get_registry().get(subsector)
    s = get_settings()

    async def go() -> dict[str, Any]:
        http = HttpClient(s)
        try:
            counts = await CensusCbpClient(http, s).establishments(sub.naics, state, level)
        finally:
            await http.aclose()
        return assess_scope(counts, s.long_list_target)

    try:
        result = _run(go())
    except CensusKeyRequired as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    table = Table(title=f"{sub.name} (NAICS {sub.naics}) - establishments by size, CBP")
    for col in ("geography", "5-9 emp", "10-19 emp", "5-19 emp", "all"):
        table.add_column(col, justify="right" if "emp" in col or col == "all" else "left")
    for row in result["top"]:
        table.add_row(
            row["geo"],
            str(row["estab_5_9"]),
            str(row["estab_10_19"]),
            str(row["estab_5_19"]),
            str(row["estab_all"]),
        )
    console.print(table)
    console.print(
        f"total 5-19 employee establishments in scope: [bold]{result['total_estab_5_19']}[/bold]; "
        f"recommended scope: [bold]{result['recommendation']}[/bold] - {result['note']}"
    )
    if not result["top"]:
        console.print(
            "[yellow]Census returned no rows.[/yellow] That is almost always a query problem, not "
            "zero businesses. Try `--level state`, or run the campaign anyway — this command is "
            "only a size estimate."
        )


@datasets_app.command("list")
def datasets_list() -> None:
    """List the configured government / public datasets (config/public_datasets.yaml)."""
    from .sources.public_datasets import load_public_datasets

    table = Table(title="Public datasets")
    for col in ("key", "kind", "type", "states", "role", "tags"):
        table.add_column(col)
    for ds in load_public_datasets():
        roles = [r for r, on in (("long_list", ds.long_list), ("owner_lookup", ds.owner_lookup)) if on]
        if ds.tags:
            roles.append("cross_check")
        table.add_row(
            ds.key, ds.kind, ds.type, ",".join(ds.states) or "-", "+".join(roles), ", ".join(ds.tags)
        )
    console.print(table)


@datasets_app.command("check")
def datasets_check(key: list[str] | None = typer.Argument(None, help="Dataset keys (default: all)")) -> None:
    """Hit every configured dataset endpoint once to verify it is still online and the columns exist."""
    from .sources.public_datasets import load_public_datasets, socrata_metadata_url

    s = get_settings()
    wanted = set(key or [])

    async def go() -> list[tuple[str, str, str]]:
        http = HttpClient(s)
        out: list[tuple[str, str, str]] = []
        try:
            for ds in load_public_datasets():
                if wanted and ds.key not in wanted:
                    continue
                try:
                    if ds.type == "socrata":
                        meta_url = socrata_metadata_url(ds.url)
                        cols: set[str] = set()
                        if meta_url:
                            meta = await http.get_json(meta_url, use_cache=False)
                            cols = {c["fieldName"] for c in meta.get("columns", []) if c.get("fieldName")}
                        if not cols:  # fall back to a sample row (nulls are omitted, so only a hint)
                            rows = await http.get_json(ds.url, params={"$limit": 1}, use_cache=False)
                            cols = set(rows[0]) if rows else set()
                        expected = (
                            set(ds.columns.values())
                            | {
                                c
                                for c in (ds.naics_column, ds.state_column, ds.city_column, ds.keyword_column)
                                if c
                            }
                            | ({ds.dba_column} if ds.dba_column else set())
                        )
                        missing = sorted(c for c in expected if c not in cols) if cols else []
                        rows = await http.get_json(
                            ds.url,
                            params={"$limit": 1, **({"$where": ds.filters} if ds.filters else {})},
                            use_cache=False,
                        )
                        status = "ok" if not missing else "columns missing"
                        detail = (
                            ", ".join(missing) or f"{len(cols)} columns; filter ok ({len(rows)} sample row)"
                        )
                        out.append((ds.key, status, detail))
                    else:
                        resp = await http.get(ds.url, use_cache=False, max_bytes=4096)
                        out.append(
                            (
                                ds.key,
                                "ok" if resp.status_code < 400 else f"http {resp.status_code}",
                                resp.headers.get("content-type", ""),
                            )
                        )
                except Exception as exc:  # noqa: BLE001
                    out.append((ds.key, "error", str(exc)[:120]))
        finally:
            await http.aclose()
        return out

    table = Table(title="Dataset health")
    for col in ("dataset", "status", "detail"):
        table.add_column(col)
    for k, status, detail in _run(go()):
        colour = "green" if status == "ok" else "red"
        table.add_row(k, f"[{colour}]{status}[/{colour}]", detail)
    console.print(table)


@datasets_app.command("ppp-download")
def datasets_ppp_download(
    dest: Path | None = typer.Option(None, help="Target folder (default ZAHEEN_PPP_DATA_DIR)"),
    list_only: bool = typer.Option(False, "--list", help="Only list the CSV resources"),
) -> None:
    """Download the SBA PPP FOIA CSVs (about 11M rows, ~8 GB) from data.sba.gov."""
    from .sources.ppp_loans import PPP_SEARCH_QUERY, PPP_SEARCH_URL, ppp_distributions

    s = get_settings()
    target = dest or s.ppp_data_dir

    async def go() -> None:
        http = HttpClient(s)
        try:
            data = await http.get_json(PPP_SEARCH_URL, params={"fulltext": PPP_SEARCH_QUERY}, use_cache=False)
            resources = ppp_distributions(data)
            if not resources:
                raise typer.BadParameter("no public_*.csv distributions found on data.sba.gov")
            for r in resources:
                console.print(f"{r['title']}: {r['url']}")
            if list_only:
                return
            target.mkdir(parents=True, exist_ok=True)
            for r in resources:
                out = target / Path(r["url"]).name
                if out.exists():
                    console.print(f"exists, skipping {out.name}")
                    continue
                console.print(f"downloading {out.name} ...")
                await http.download(r["url"], out)
            console.print(
                f"done. {len(resources)} files in {target}; the first campaign run builds the state index."
            )
        finally:
            await http.aclose()

    _run(go())


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


@campaign_app.command("test")
def campaign_test(
    subsector: str = typer.Argument("plumbing_hvac", help="Sub-sector key"),
    location: str = typer.Option("Austin, TX", "--location", "-l"),
    companies: int = typer.Option(200, "--companies", "-n", help="Long-list size to collect and enrich"),
    output: Path = typer.Option(Path("data/test-200.xlsx"), help="Spreadsheet to write"),
) -> None:
    """Collect, enrich, score and export a small campaign (default 200 companies). No Google Places."""
    sub = get_registry().get(subsector)
    spec = CampaignSpec(
        name=f"test {companies} {sub.name} - {location}",
        subsector_key=subsector,
        geography=_geo([location], None, "US", 25.0),
        long_list_target=companies,
        short_list_target=min(100, companies),
        enrich=True,
        contact_enrichment=False,
    )

    async def go() -> None:
        db = Database()
        await db.init()
        http = HttpClient()
        c = await db.create_campaign(spec)
        console.print(f"created campaign [bold]{c.id}[/bold]: {spec.name}")
        runner = CampaignRunner(db, http, progress=_progress)
        result = await runner.run(c, start_stage="collect", stop_after="score")
        items = await db.list_businesses(c.id)
        with_site = sum(1 for b in items if b.website)
        with_addr = sum(1 for b in items if b.address and b.address.line1)
        with_phone = sum(1 for b in items if b.phone_e164)
        selected = [b for b in items if b.status == BusinessStatus.SELECTED]
        path = write_export(output, selected or items)
        await http.aclose()
        await db.close()
        console.print_json(json.dumps(result.stats, default=str))
        console.print(
            f"state={result.state.value} businesses={len(items)} "
            f"website={with_site} address={with_addr} phone={with_phone} "
            f"selected={len(selected)} wrote {path}"
        )

    _run(go())


def _progress(stage: str, data: dict[str, Any]) -> None:
    console.print(f"[cyan]{stage}[/cyan] " + " ".join(f"{k}={v}" for k, v in data.items()))
    _flush()


@campaign_app.command("run")
def campaign_run(
    campaign_id: str,
    start_stage: str = typer.Option("collect", help=f"One of {STAGES}"),
    stop_after: str | None = typer.Option(
        None, help=f"Stop after this stage ({', '.join(STAGES)}). Use 'score' to skip Google Places."
    ),
    import_file: list[Path] | None = typer.Option(
        None, "--import", help="CSV/XLSX files (registry extracts, member lists) to merge in"
    ),
) -> None:
    """Run (or resume) a campaign in this process."""
    through = stop_after or "finalize"
    console.print(
        f"starting campaign [bold]{campaign_id}[/bold] at [cyan]{start_stage}[/cyan] "
        f"through [cyan]{through}[/cyan]. Progress prints while it works; "
        "200 companies take a while — wait for state=scored before exporting."
    )
    _flush()

    async def go() -> None:
        db = Database()
        http = HttpClient()
        ctx = SourceContext(http=http, settings=get_settings())
        extra: list[Source] = [
            CsvImportSource(ctx, p, source_label=f"import:{p.stem}", kind=SourceKind.IMPORT)
            for p in (import_file or [])
        ]
        try:
            await db.init()
            c = await db.get_campaign(campaign_id)
            if not c:
                raise typer.BadParameter(f"campaign {campaign_id} not found")
            console.print(
                f"loaded [bold]{c.spec.name}[/bold] state={c.state.value} stage={c.stage or '-'}"
            )
            _flush()
            runner = CampaignRunner(db, http, extra_sources=extra, progress=_progress)
            result = await runner.run(c, start_stage, stop_after)
        except Exception as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "database locked" in msg:
                console.print(
                    "[red]another zaheen process has data/zaheen.db open. "
                    "In a second tab run: pgrep -fl zaheen[/red]"
                )
            raise
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
