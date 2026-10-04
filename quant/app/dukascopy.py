"""Dukascopy public datafeed adapter (free, no account): per-day M1 BID candle files.

URL:  https://datafeed.dukascopy.com/datafeed/{INST}/{YYYY}/{MM-1, zero-based}/{DD}/BID_candles_min_1.bi5
File: LZMA stream of 24-byte big-endian records  (uint32 seconds-from-day-start, uint32 open, uint32 close,
      uint32 low, uint32 high, float32 volume); prices are integer points (XAUUSD/XAGUSD/JPY pairs: /1000,
      other FX: /100000). Verified against live files.

The feed rate-limits hard (HTTP 429 after a handful of requests). We never try to get around that: requests
are spaced, concurrency is low, a 429 opens a circuit breaker, every downloaded day is cached on disk forever
and callers simply use whatever is cached (plus the Yahoo fallback). A background backfill thread fills the
cache newest-day-first at a polite pace.
"""
from __future__ import annotations

import logging
import lzma
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
import numpy as np
import pandas as pd

from .symbols import Symbol

log = logging.getLogger("quant.dukascopy")

BASE = "https://datafeed.dukascopy.com/datafeed"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "dukascopy"
RECORD = np.dtype([("t", ">u4"), ("o", ">u4"), ("c", ">u4"), ("l", ">u4"), ("h", ">u4"), ("v", ">f4")])
MAX_CONCURRENCY = 2  # (spec allows <= 4; the feed throttles, so stay lower)
MIN_SPACING_S = 5.0
BLOCK_BASE_S, BLOCK_MAX_S = 15 * 60, 2 * 3600
FETCH_BUDGET_PER_CALL = 0  # API requests never wait on the (slow, throttled) feed: the backfill thread fills the cache

_FX = {"EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "USDJPY", "EURJPY", "GBPJPY", "AUDJPY",
       "CHFJPY", "CADJPY", "EURGBP"}


def instrument(sym: Symbol) -> Optional[str]:
    if sym.id in _FX or sym.id in ("XAUUSD", "XAGUSD"):
        return sym.id
    return None


def point_divisor(inst: str) -> float:
    if inst in ("XAUUSD", "XAGUSD") or inst.endswith("JPY"):
        return 1000.0
    return 100000.0


def day_url(inst: str, d: date) -> str:
    return f"{BASE}/{inst}/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5"


def decode_bi5(blob: bytes, d: date, divisor: float) -> pd.DataFrame:
    """M1 candles for one UTC day. Minutes without trades (volume 0, flat) are dropped."""
    cols = ["o", "h", "l", "c", "v"]
    if not blob:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    raw = lzma.decompress(blob)
    if len(raw) % RECORD.itemsize:
        raise ValueError(f"bi5: {len(raw)} bytes is not a multiple of {RECORD.itemsize}")
    a = np.frombuffer(raw, dtype=RECORD)
    a = a[a["v"] > 0]
    base = pd.Timestamp(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))
    idx = base + pd.to_timedelta(a["t"].astype("int64"), unit="s")
    df = pd.DataFrame({"o": a["o"] / divisor, "h": a["h"] / divisor, "l": a["l"] / divisor,
                       "c": a["c"] / divisor, "v": a["v"].astype(float)}, index=pd.DatetimeIndex(idx).as_unit("ns"))
    return df[cols]


def encode_bi5(rows: list[tuple], divisor: float) -> bytes:
    """Inverse of decode_bi5 (tests): rows = (seconds, o, h, l, c, v) in price units."""
    a = np.zeros(len(rows), dtype=RECORD)
    for i, (t, o, h, l, c, v) in enumerate(rows):
        a[i] = (t, round(o * divisor), round(c * divisor), round(l * divisor), round(h * divisor), v)
    return lzma.compress(a.tobytes(), format=lzma.FORMAT_ALONE)


# ------------------------------------------------------------------ polite fetching + disk cache

_sem = threading.Semaphore(MAX_CONCURRENCY)
_lock = threading.Lock()
_state = {"last_request": 0.0, "blocked_until": 0.0, "block_s": BLOCK_BASE_S, "last_error": None,
          "requests": 0, "downloaded": 0}


def _path(inst: str, d: date) -> Path:
    return CACHE_DIR / inst / f"{d.isoformat()}.bi5"


def cached(inst: str, d: date) -> bool:
    return _path(inst, d).exists()


def blocked() -> Optional[float]:
    """Seconds until the circuit breaker closes (None if open for business)."""
    rem = _state["blocked_until"] - time.time()
    return rem if rem > 0 else None


def fetch_day(inst: str, d: date) -> bool:
    """Download one day into the disk cache. Returns True if the file is (now) cached."""
    p = _path(inst, d)
    if p.exists():
        return True
    if blocked():
        return False
    with _sem:
        with _lock:
            wait = MIN_SPACING_S - (time.time() - _state["last_request"])
            _state["last_request"] = time.time() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)
        if blocked():
            return False
        try:
            _state["requests"] += 1
            r = httpx.get(day_url(inst, d), timeout=40)
        except Exception as e:
            with _lock:  # unreachable / hanging feed: back off a little instead of retrying on every call
                _state["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                _state["blocked_until"] = time.time() + 5 * 60
            return False
        if r.status_code == 429:
            with _lock:
                _state["blocked_until"] = time.time() + _state["block_s"]
                _state["last_error"] = f"HTTP 429 rate limit; pausing {int(_state['block_s'] / 60)} min"
                _state["block_s"] = min(_state["block_s"] * 2, BLOCK_MAX_S)
            log.warning("dukascopy rate-limited; backing off")
            return False
        if r.status_code == 404:
            body = b""
        elif r.status_code == 200:
            body = r.content
            try:
                decode_bi5(body, d, 1.0)
            except Exception as e:
                _state["last_error"] = f"undecodable file for {d}: {e}"
                return False
        else:
            _state["last_error"] = f"HTTP {r.status_code}"
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(body)
        tmp.replace(p)
        with _lock:
            _state["downloaded"] += 1
            _state["block_s"] = BLOCK_BASE_S
        return True


def wanted_days(end: date, history_days: int) -> list[date]:
    """Calendar days (newest first) that can hold FX data: Saturdays are skipped."""
    return [d for d in (end - timedelta(days=i) for i in range(history_days + 1)) if d.weekday() != 5]


def load_m1(inst: str, end: date, history_days: int, budget: int = FETCH_BUDGET_PER_CALL) -> tuple[pd.DataFrame, dict]:
    """M1 frame from the most recent CONTIGUOUS run of cached days ending at `end` (newest first), fetching at
    most `budget` missing files on the way. Returns (frame, coverage)."""
    days = wanted_days(end, history_days)
    div = point_divisor(inst)
    frames, used, fetched = [], [], 0
    for d in days:
        if not cached(inst, d):
            if fetched >= budget or not fetch_day(inst, d):
                break
            fetched += 1
        try:
            frames.append(decode_bi5(_path(inst, d).read_bytes(), d, div))
            used.append(d)
        except Exception as e:  # corrupt cache file: drop it and stop the run here
            log.warning("bad cached file %s %s: %s", inst, d, e)
            _path(inst, d).unlink(missing_ok=True)
            break
    cov = {"instrument": inst, "days_wanted": len(days), "days_used": len(used),
           "from": used[-1].isoformat() if used else None, "to": used[0].isoformat() if used else None,
           "complete": len(used) == len(days), **status()}
    if not frames:
        return pd.DataFrame(columns=["o", "h", "l", "c", "v"], index=pd.DatetimeIndex([], tz="UTC")), cov
    df = pd.concat(frames[::-1])
    return df[~df.index.duplicated()].sort_index(), cov


def status() -> dict:
    b = blocked()
    return {"rate_limited": b is not None, "retry_in_minutes": round(b / 60, 1) if b else None,
            "last_error": _state["last_error"], "requests_this_run": _state["requests"],
            "files_downloaded_this_run": _state["downloaded"]}


def cache_status(inst: str) -> dict:
    p = CACHE_DIR / inst
    files = sorted(x.stem for x in p.glob("*.bi5")) if p.exists() else []
    return {"instrument": inst, "cached_days": len(files), "first": files[0] if files else None,
            "last": files[-1] if files else None, **status()}


# ------------------------------------------------------------------ background backfill

_jobs: dict[str, threading.Thread] = {}


def start_backfill(inst: str, end: date, history_days: int) -> bool:
    """Fill the cache newest-day-first in a daemon thread; stops for good on a rate limit (the next call
    after the pause resumes). Returns True if a job is running."""
    t = _jobs.get(inst)
    if t and t.is_alive():
        return True
    if blocked():
        return False

    def run():
        for d in wanted_days(end, history_days):
            if cached(inst, d):
                continue
            if blocked() or not fetch_day(inst, d):
                break
    t = threading.Thread(target=run, name=f"duka-backfill-{inst}", daemon=True)
    _jobs[inst] = t
    t.start()
    return True


def prefetch_days(inst: str, days: list[date]) -> None:
    """Fetch a few specific days in the background (no-op while rate-limited or already cached/in flight)."""
    todo = [d for d in days if d.weekday() != 5 and not cached(inst, d) and d < datetime.now(timezone.utc).date()]
    key = f"{inst}:prefetch"
    t = _jobs.get(key)
    if not todo or blocked() or (t and t.is_alive()):
        return

    def run():
        for d in todo[:3]:
            if blocked() or not fetch_day(inst, d):
                break
    t = threading.Thread(target=run, name=f"duka-prefetch-{inst}", daemon=True)
    _jobs[key] = t
    t.start()
