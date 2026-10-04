from datetime import datetime, timezone

from app.sessions import fx_market_open, get_sessions


def sess(now):
    return {s["name"]: s for s in get_sessions(now)["sessions"]}


def test_london_dst_shift():
    summer = sess(datetime(2025, 7, 15, 12, 0, tzinfo=timezone.utc))
    winter = sess(datetime(2025, 1, 15, 12, 0, tzinfo=timezone.utc))
    assert summer["London"]["open_utc"].startswith("2025-07-15T07:00")  # BST
    assert winter["London"]["open_utc"].startswith("2025-01-15T08:00")  # GMT
    assert summer["New York"]["open_utc"].startswith("2025-07-15T12:00")  # EDT
    assert winter["New York"]["open_utc"].startswith("2025-01-15T13:00")  # EST
    assert summer["London"]["dst_active"] and not winter["London"]["dst_active"]


def test_sydney_southern_dst():
    jan = sess(datetime(2025, 1, 15, 0, 0, tzinfo=timezone.utc))  # AEDT (UTC+11)
    jul = sess(datetime(2025, 7, 15, 0, 0, tzinfo=timezone.utc))  # AEST (UTC+10)
    assert jan["Sydney"]["active"] and jan["Sydney"]["open_utc"].startswith("2025-01-14T20:00")
    assert jul["Sydney"]["open_utc"].startswith("2025-07-14T21:00")


def test_overlap_and_active():
    now = datetime(2025, 7, 15, 13, 30, tzinfo=timezone.utc)
    r = get_sessions(now)
    assert set(r["active"]) == {"London", "New York"}
    assert ["London", "New York"] in r["current_overlaps"]
    assert r["fx_market_open"]


def test_weekend():
    sat = datetime(2025, 7, 19, 12, 0, tzinfo=timezone.utc)
    r = get_sessions(sat)
    assert not r["fx_market_open"] and r["active"] == []
    assert r["next_session"]["name"] == "Sydney"
    assert r["next_session"]["open_utc"].startswith("2025-07-20T21:00")  # Monday 07:00 AEST
    assert fx_market_open(datetime(2025, 7, 20, 21, 30, tzinfo=timezone.utc))  # Sun 17:30 NY
    assert not fx_market_open(datetime(2025, 7, 18, 21, 30, tzinfo=timezone.utc))  # Fri 17:30 NY
