from datetime import datetime, timezone

from app.calendar import filter_events

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
EVENTS = [
    {"title": "CPI", "country": "USD", "date": "2026-09-30T08:30:00-04:00", "impact": "High", "forecast": "0.3%", "previous": "0.2%"},
    {"title": "Old", "country": "USD", "date": "2026-09-28T08:30:00-04:00", "impact": "High", "forecast": "", "previous": ""},
    {"title": "BoJ", "country": "JPY", "date": "2026-10-01T03:00:00+00:00", "impact": "Medium", "forecast": "", "previous": ""},
    {"title": "Low thing", "country": "USD", "date": "2026-10-01T03:00:00+00:00", "impact": "Low", "forecast": "", "previous": ""},
    {"title": "EUR thing", "country": "EUR", "date": "2026-10-01T03:00:00+00:00", "impact": "High", "forecast": "", "previous": ""},
]


def test_filter_and_status():
    ev = filter_events(EVENTS, NOW, {"USD", "JPY"}, {"high", "medium"}, days=7)
    assert [e["title"] for e in ev] == ["CPI", "BoJ"]
    cpi, boj = ev
    assert cpi["time_utc"] == "2026-09-30T12:30:00Z" and cpi["status"] == "upcoming" and cpi["minutes_until"] == 30
    assert boj["status"] == "upcoming"
    ev2 = filter_events(EVENTS, datetime(2026, 9, 30, 13, 0, tzinfo=timezone.utc), {"USD"}, {"high"}, days=1)
    assert ev2[0]["status"] == "recent" and ev2[0]["minutes_until"] == -30
