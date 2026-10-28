from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own data dir / cache / database and no API keys."""
    for key in list(os.environ):
        if key.startswith("ZAHEEN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ZAHEEN_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ZAHEEN_HTTP_CACHE_PATH", str(tmp_path / "data" / "cache.sqlite"))
    monkeypatch.setenv("ZAHEEN_PPP_DATA_DIR", str(tmp_path / "data" / "ppp"))
    monkeypatch.setenv("ZAHEEN_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'data' / 'test.db'}")
    monkeypatch.setenv("ZAHEEN_ENABLE_OVERPASS", "false")
    monkeypatch.setenv("ZAHEEN_PER_HOST_REQUESTS_PER_SECOND", "1000")
    from zaheendata import settings as settings_mod

    settings_mod.get_settings.cache_clear()
    yield
    settings_mod.get_settings.cache_clear()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES
