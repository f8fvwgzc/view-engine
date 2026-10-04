"""Economic calendar from the free ForexFactory (faireconomy.media) weekly JSON feeds."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

import httpx
import pandas as pd

from .cache import TTL_CALENDAR, cache
from .data import UA, DataError, iso, now_utc, replay_as_of

FEEDS = {
    "thisweek": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "nextweek": "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
}
SOURCE = "ForexFactory weekly JSON via nfs.faireconomy.media"


CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache"
FAIL_TTL = 5 * 60  # back off this long after an upstream error (the feed rate-limits aggressively)
STALE_MAX = timedelta(hours=36)


def _disk_path(name: str) -> Path:
    return CACHE_DIR / f"ff_{name}.json"


def _read_disk(name: str) -> Optional[dict]:
    try:
        d = json.loads(_disk_path(name).read_text())
        d["fetched_at"] = datetime.fromisoformat(d["fetched_at"])
        return d
    except Exception:
        return None


def _write_disk(name: str, feed: dict) -> None:
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        _disk_path(name).write_text(json.dumps({**feed, "fetched_at": feed["fetched_at"].isoformat()}))
    except Exception:
        pass


def _fetch(name: str) -> dict:
    """Fetch one weekly feed: memory cache (30 min) -> HTTP -> on error, last good copy from disk."""
    key = ("ffcal", name)
    hit = cache.get(key)
    if hit is not None:
        return hit
    now = datetime.now(timezone.utc)
    disk = _read_disk(name)
    if disk and now - disk["fetched_at"] < timedelta(seconds=TTL_CALENDAR):
        cache.set(key, disk, TTL_CALENDAR - (now - disk["fetched_at"]).total_seconds())
        return disk
    try:
        r = httpx.get(FEEDS[name], headers={"User-Agent": UA}, timeout=15)
        if r.status_code == 404:
            feed = {"ok": False, "status": 404, "events": [], "fetched_at": now}
        else:
            r.raise_for_status()
            feed = {"ok": True, "status": 200, "events": r.json(), "fetched_at": now}
            _write_disk(name, feed)
        cache.set(key, feed, TTL_CALENDAR)
        return feed
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:160]}"
        if disk and disk.get("ok") and now - disk["fetched_at"] < STALE_MAX:
            feed = {**disk, "stale": True, "error": err}
        else:
            feed = {"ok": False, "status": "error", "events": [], "fetched_at": now, "error": err}
        cache.set(key, feed, FAIL_TTL)
        return feed


def normalize_event(e: dict, now: datetime) -> Optional[dict]:
    try:
        t = pd.Timestamp(e["date"]).tz_convert("UTC").to_pydatetime()
    except Exception:
        return None
    mins = (t - now).total_seconds() / 60
    return {
        "time_utc": iso(t), "currency": (e.get("country") or "").upper(), "title": e.get("title"),
        "impact": e.get("impact"), "forecast": e.get("forecast") or None, "previous": e.get("previous") or None,
        "minutes_until": round(mins), "status": "upcoming" if mins >= 0 else "recent",
    }


def filter_events(events: Iterable[dict], now: datetime, currencies: Optional[set[str]], impacts: Optional[set[str]],
                  days: float, past_hours: float = 24) -> list[dict]:
    lo, hi = now - timedelta(hours=past_hours), now + timedelta(days=days)
    out, seen = [], set()
    for raw in events:
        ev = normalize_event(raw, now)
        if ev is None:
            continue
        t = pd.Timestamp(ev["time_utc"]).to_pydatetime()
        if not (lo <= t <= hi):
            continue
        if currencies and ev["currency"] not in currencies:
            continue
        if impacts and (ev["impact"] or "").lower() not in impacts:
            continue
        key = (ev["time_utc"], ev["currency"], ev["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append(ev)
    out.sort(key=lambda x: x["time_utc"])
    return out


def get_calendar(currencies: Optional[list[str]] = None, impacts: Optional[list[str]] = None, days: float = 7,
                 past_hours: float = 24, now: Optional[datetime] = None) -> dict:
    now = now or now_utc()
    cur = {c.strip().upper() for c in currencies or [] if c.strip()} or None
    imp = {i.strip().lower() for i in impacts or [] if i.strip()} or None
    raw, notes, fetched = [], [], []
    for name in ("thisweek", "nextweek"):
        feed = _fetch(name)
        if feed.get("stale"):
            notes.append(f"{name}: upstream error ({feed['error']}); serving cached copy from "
                         f"{iso(feed['fetched_at'])}")
        if feed["ok"]:
            fetched.append(feed["fetched_at"])
            raw.extend(feed["events"])
        elif feed["status"] == 404:
            notes.append(f"{name} feed not published yet (HTTP 404)")
        else:
            notes.append(f"{name} feed unavailable: {feed.get('error')}")
            if name == "thisweek":
                raise DataError(f"economic calendar unavailable: {feed.get('error')} (retry in a few minutes)")
    evs = filter_events(raw, now, cur, imp, days, past_hours)
    coverage = None
    if raw:
        times = [pd.Timestamp(e["date"]).tz_convert("UTC") for e in raw if e.get("date")]
        coverage = iso(max(times)) if times else None
        if times and max(times).to_pydatetime() < now + timedelta(days=days):
            notes.append(f"feed only covers events through {coverage}; later events unknown")
    if replay_as_of() is not None:
        first = min((pd.Timestamp(e["date"]).tz_convert("UTC") for e in raw if e.get("date")), default=None)
        if first is None or pd.Timestamp(now) < first - pd.Timedelta(days=1):
            evs = []
            notes.append("replay: the free feed only holds the current week, so the calendar for this past date "
                         "is UNAVAILABLE (not 'no events')")
        else:
            notes.append("replay: events are from the current weekly feed, timed relative to the replay timestamp")
    return {
        "source": SOURCE, "as_of": iso(min(fetched)) if fetched else iso(now), "now_utc": iso(now),
        "filters": {"currencies": sorted(cur) if cur else "all", "impact": sorted(imp) if imp else "all",
                    "days_ahead": days, "past_hours": past_hours},
        "coverage_until": coverage, "count": len(evs), "events": evs, "notes": notes,
    }
