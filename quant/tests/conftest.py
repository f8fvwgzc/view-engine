import pytest


@pytest.fixture(autouse=True)
def _isolate_disk_caches(tmp_path, monkeypatch):
    """Tests never read or write the real on-disk caches (Dukascopy days, calendar feed)."""
    from app import calendar, data
    from app import dukascopy as dk
    monkeypatch.setattr(dk, "CACHE_DIR", tmp_path / "dukascopy")
    monkeypatch.setattr(calendar, "CACHE_DIR", tmp_path / "cal")
    data._anchor_cache.clear()
    yield
    data._anchor_cache.clear()
