import pytest


@pytest.fixture(autouse=True)
def _isolate_disk_caches(tmp_path, monkeypatch):
    """Tests never read or write the real on-disk caches (M1 store, Dukascopy days, calendar feed, exports)."""
    from app import calendar, data, m1store
    from app import dukascopy as dk
    monkeypatch.setattr(dk, "CACHE_DIR", tmp_path / "dukascopy")
    monkeypatch.setattr(calendar, "CACHE_DIR", tmp_path / "cal")
    monkeypatch.setattr(m1store, "STORE_DIR", tmp_path / "m1")
    monkeypatch.setattr(m1store, "ZIP_DIR", tmp_path / "zips")
    monkeypatch.setattr(m1store, "IMPORT_DIR", tmp_path / "import")
    m1store._mem.clear()
    data._anchor_cache.clear()
    try:
        from app import dataset
        monkeypatch.setattr(dataset, "EXPORT_DIR", tmp_path / "exports", raising=False)
    except Exception:
        pass
    yield
    m1store._mem.clear()
    data._anchor_cache.clear()
