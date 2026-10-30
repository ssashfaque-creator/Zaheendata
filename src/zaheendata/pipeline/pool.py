from __future__ import annotations

import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from zaheendata.checkpoint import HarvestState
from zaheendata.warehouse import connect

IN_PROGRESS = {"running", "paused", "crawling"}


def partition_trades(
    keys: list[str],
    subsector_state: dict,
    workers: int,
) -> list[list[str]]:
    """Put an in-progress trade on worker 0. Split untouched trades across the rest."""
    workers = max(1, int(workers))
    active = [key for key in keys if subsector_state.get(key, {}).get("status") != "complete"]
    in_progress = [key for key in active if subsector_state.get(key, {}).get("status") in IN_PROGRESS]
    pending = [key for key in active if key not in in_progress]
    buckets: list[list[str]] = [[] for _ in range(workers)]
    if in_progress:
        buckets[0].extend(in_progress)
        others = list(range(1, workers)) or [0]
        for index, key in enumerate(pending):
            buckets[others[index % len(others)]].append(key)
    else:
        for index, key in enumerate(pending):
            buckets[index % workers].append(key)
    return buckets


def copy_warehouse(src: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.copy2(src, dest)
    src_wal = Path(str(src) + ".wal")
    dest_wal = Path(str(dest) + ".wal")
    if src_wal.exists():
        shutil.copy2(src_wal, dest_wal)
    elif dest_wal.exists():
        dest_wal.unlink()
    return dest


def worker_has_progress(state_path: Path) -> bool:
    if not state_path.exists():
        return False
    state = HarvestState.load(state_path)
    return any(
        row.get("status") in {"running", "paused", "complete"}
        for row in state.data.get("subsectors", {}).values()
    )


def merge_worker_warehouse(main: Path, worker: Path) -> dict[str, int]:
    if not worker.exists() or worker.resolve() == main.resolve():
        return {"snapshots": 0, "technologies": 0, "leads": 0}
    con = connect(main)
    try:
        con.execute(f"ATTACH '{worker.as_posix()}' AS worker")
        before_snap = int(con.execute("SELECT count(*) FROM snapshots").fetchone()[0])
        before_tech = int(con.execute("SELECT count(*) FROM technologies").fetchone()[0])
        before_leads = int(con.execute("SELECT count(*) FROM leads").fetchone()[0])
        con.execute("INSERT INTO snapshots SELECT * FROM worker.snapshots")
        con.execute("INSERT INTO technologies SELECT * FROM worker.technologies")
        con.execute("INSERT OR REPLACE INTO leads SELECT * FROM worker.leads")
        con.execute("CHECKPOINT")
        after_snap = int(con.execute("SELECT count(*) FROM snapshots").fetchone()[0])
        after_tech = int(con.execute("SELECT count(*) FROM technologies").fetchone()[0])
        after_leads = int(con.execute("SELECT count(*) FROM leads").fetchone()[0])
        con.execute("DETACH worker")
    finally:
        con.close()
    return {
        "snapshots": after_snap - before_snap,
        "technologies": after_tech - before_tech,
        "leads": after_leads - before_leads,
    }


def run_trade_pool(
    *,
    geo: str,
    assignments: list[list[str]],
    warehouse: Path,
    raw_dir: Path,
    out_dir: Path,
    state_path: Path,
    log_path: Path,
    keep: int,
    limit: int | None,
    crawl: str,
    concurrency: int,
    progress,
    stop,
) -> dict:
    worker_dir = raw_dir.parent / "workers"
    worker_dir.mkdir(parents=True, exist_ok=True)
    children: list[dict] = []
    python = sys.executable

    for index, trades in enumerate(assignments):
        if not trades:
            progress.emit(f"worker {index}: idle")
            continue
        if index == 0:
            # In-progress trade stays on the main warehouse so snapshots resume.
            child_warehouse = warehouse
            child_state = state_path
            child_log = log_path
        else:
            child_warehouse = worker_dir / f"w{index}.duckdb"
            child_state = worker_dir / f"w{index}_state.json"
            child_log = worker_dir / f"w{index}.log"
            if not (child_warehouse.exists() and worker_has_progress(child_state)):
                progress.emit(f"worker {index}: copying warehouse → {child_warehouse}")
                copy_warehouse(warehouse, child_warehouse)
        command = [
            python,
            "-m",
            "zaheendata",
            "harvest",
            "--geo",
            geo,
            "--warehouse",
            str(child_warehouse),
            "--raw-dir",
            str(raw_dir),
            "--out-dir",
            str(out_dir),
            "--keep",
            str(keep),
            "--crawl",
            crawl,
            "--concurrency",
            str(concurrency),
            "--skip-setup",
            "--state",
            str(child_state),
            "--log",
            str(child_log),
            "--subsector",
            *trades,
        ]
        if limit:
            command.extend(["--limit", str(limit)])
        progress.emit(f"worker {index}: {' '.join(trades)}  log={child_log}")
        proc = subprocess.Popen(command)
        children.append(
            {
                "index": index,
                "trades": trades,
                "proc": proc,
                "warehouse": child_warehouse,
                "state": child_state,
                "log": child_log,
            }
        )

    codes: dict[int, int] = {}
    try:
        while children and any(item["proc"].poll() is None for item in children):
            if stop.requested:
                for item in children:
                    if item["proc"].poll() is None:
                        item["proc"].send_signal(signal.SIGINT)
                break
            time.sleep(1)
        for item in children:
            if item["proc"].poll() is None:
                item["proc"].wait()
            codes[item["index"]] = item["proc"].returncode or 0
    finally:
        merged = []
        for item in children:
            if item["warehouse"].resolve() == warehouse.resolve():
                continue
            stats = merge_worker_warehouse(warehouse, item["warehouse"])
            merged.append({"worker": item["index"], "trades": item["trades"], **stats})
            worker_state = HarvestState.load(item["state"])
            main_state = HarvestState.load(state_path)
            for trade in item["trades"]:
                main_state.data.setdefault("subsectors", {})[trade] = worker_state.subsector(trade)
            main_state.save()

    return {
        "workers": [
            {
                "index": item["index"],
                "trades": item["trades"],
                "exit": codes.get(item["index"]),
                "log": str(item["log"]),
                "warehouse": str(item["warehouse"]),
            }
            for item in children
        ],
        "merged": merged,
    }
