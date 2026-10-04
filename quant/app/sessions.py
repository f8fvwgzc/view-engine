"""FX trading sessions with DST-aware open/close times (zoneinfo)."""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .data import iso

SESSIONS = [
    ("Sydney", "Australia/Sydney", time(7, 0), time(16, 0)),
    ("Tokyo", "Asia/Tokyo", time(9, 0), time(18, 0)),
    ("London", "Europe/London", time(8, 0), time(17, 0)),
    ("New York", "America/New_York", time(8, 0), time(17, 0)),
]
NY = ZoneInfo("America/New_York")


def fx_market_open(now: datetime) -> bool:
    """Spot FX: Sunday 17:00 New York -> Friday 17:00 New York."""
    ny = now.astimezone(NY)
    wd, t = ny.weekday(), ny.time()
    if wd == 5:  # Saturday
        return False
    if wd == 6:  # Sunday
        return t >= time(17, 0)
    if wd == 4:  # Friday
        return t < time(17, 0)
    return True


def _occurrences(tz: ZoneInfo, open_t: time, close_t: time, now: datetime, days=range(-2, 9)):
    """Session windows (UTC) on local weekdays Mon-Fri around `now`."""
    local_today = now.astimezone(tz).date()
    for d in days:
        day = local_today + timedelta(days=d)
        if day.weekday() >= 5:
            continue
        o = datetime.combine(day, open_t, tz).astimezone(timezone.utc)
        c = datetime.combine(day, close_t, tz).astimezone(timezone.utc)
        yield o, c


def get_sessions(now: Optional[datetime] = None) -> dict:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    out, active = [], []
    windows: dict[str, tuple[datetime, datetime]] = {}
    for name, tzname, ot, ct in SESSIONS:
        tz = ZoneInfo(tzname)
        occ = list(_occurrences(tz, ot, ct, now))
        cur = next(((o, c) for o, c in occ if o <= now < c), None)
        nxt = next(((o, c) for o, c in occ if o > now), None)
        today = cur or nxt
        is_active = cur is not None
        if is_active:
            active.append(name)
        windows[name] = today
        loc = now.astimezone(tz)
        out.append({
            "name": name, "timezone": tzname, "local_hours": f"{ot:%H:%M}-{ct:%H:%M}",
            "dst_active": bool(loc.dst()), "active": is_active,
            "open_utc": iso(today[0]) if today else None, "close_utc": iso(today[1]) if today else None,
            "minutes_to_close": round((cur[1] - now).total_seconds() / 60) if cur else None,
            "next_open_utc": iso(nxt[0]) if nxt else None,
            "minutes_to_open": round((nxt[0] - now).total_seconds() / 60) if nxt else None,
        })
    overlaps = []
    names = [s[0] for s in SESSIONS]
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = windows.get(names[i]), windows.get(names[j])
            if not a or not b:
                continue
            s, e = max(a[0], b[0]), min(a[1], b[1])
            if s < e:
                overlaps.append({"sessions": [names[i], names[j]], "start_utc": iso(s), "end_utc": iso(e),
                                 "active": s <= now < e})
    upcoming = [x for x in out if x["next_open_utc"] and not x["active"]]
    nxt = min(upcoming, key=lambda x: x["next_open_utc"]) if upcoming else None
    return {
        "now_utc": iso(now), "source": "computed (zoneinfo, IANA tz database)", "as_of": iso(now),
        "fx_market_open": fx_market_open(now), "sessions": out, "active": active,
        "current_overlaps": [o["sessions"] for o in overlaps if o["active"]], "overlaps": overlaps,
        "next_session": {"name": nxt["name"], "open_utc": nxt["next_open_utc"],
                         "minutes_to_open": nxt["minutes_to_open"]} if nxt else None,
    }
