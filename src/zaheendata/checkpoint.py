from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(path, json.dumps(payload, indent=2, default=str) + "\n")


def json_records_ready(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 3:
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, list) and len(payload) > 0


class ProgressLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, message: str) -> None:
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        line = f"[{stamp}] {message}"
        print(line, flush=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())


class StopFlag:
    """First Ctrl+C / SIGTERM finishes the current site and saves. Second forces exit."""

    def __init__(self, on_stop: Callable[[], None] | None = None) -> None:
        self._stop = False
        self._on_stop = on_stop

    @property
    def requested(self) -> bool:
        return self._stop

    def __call__(self) -> bool:
        return self._stop

    def request_stop(self, *_args: Any) -> None:
        if self._stop:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            raise KeyboardInterrupt
        self._stop = True
        print(
            "[stop] interrupt received — finishing in-flight site(s) and writing a checkpoint. "
            "Run the same command to resume. Press Ctrl+C again to force quit.",
            flush=True,
        )
        if self._on_stop:
            self._on_stop()

    def install(self) -> None:
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self.request_stop)


class HarvestState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {
            "geo": "",
            "phase": "start",
            "stopped": False,
            "overture": {},
            "licenses": {},
            "ingest": {},
            "subsectors": {},
            "updated_at": "",
        }

    @classmethod
    def load(cls, path: Path) -> HarvestState:
        state = cls(path)
        for candidate in (path, path.with_name(path.name + ".tmp")):
            if not candidate.exists():
                continue
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(loaded, dict):
                state.data.update(loaded)
                break
        return state

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(self.path, self.data)

    def set_phase(self, phase: str) -> None:
        self.data["phase"] = phase
        self.save()

    def subsector(self, key: str) -> dict[str, Any]:
        bucket = self.data.setdefault("subsectors", {})
        return bucket.setdefault(
            key,
            {
                "status": "pending",
                "discovered": 0,
                "enrichable": 0,
                "crawled": 0,
                "failed": 0,
                "selected": 0,
                "out": "",
            },
        )
