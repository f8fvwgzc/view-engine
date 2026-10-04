from datetime import date

import numpy as np
import pandas as pd
import pytest

from app import data, story
from app import structure as st
from app.cache import cache
from app.data import fx_daily_from_hourly, replay, resample_ohlc
from app.symbols import Symbol

SYM = Symbol(id="TESTFX", name="Test FX", asset_class="fx", yahoo="TESTFX=X", currencies=("USD",), known=False)


def test_session_blocks_dst():
    s = {n: (a, b) for n, a, b in story.session_blocks(date(2025, 7, 15))}       # BST / EDT
    assert s["Asia"] == (pd.Timestamp("2025-07-14 21:00", tz="UTC"), pd.Timestamp("2025-07-15 07:00", tz="UTC"))
    assert s["London"] == (pd.Timestamp("2025-07-15 07:00", tz="UTC"), pd.Timestamp("2025-07-15 12:00", tz="UTC"))
    assert s["New York"] == (pd.Timestamp("2025-07-15 12:00", tz="UTC"), pd.Timestamp("2025-07-15 21:00", tz="UTC"))
    w = {n: (a, b) for n, a, b in story.session_blocks(date(2025, 1, 15))}       # GMT / EST
    assert w["Asia"] == (pd.Timestamp("2025-01-14 22:00", tz="UTC"), pd.Timestamp("2025-01-15 08:00", tz="UTC"))
    assert w["New York"] == (pd.Timestamp("2025-01-15 13:00", tz="UTC"), pd.Timestamp("2025-01-15 22:00", tz="UTC"))
    # trading date rolls at 17:00 New York; weekends are skipped
    assert story.trade_date(pd.Timestamp("2025-07-14 21:30", tz="UTC")) == date(2025, 7, 15)
    assert story.trade_date(pd.Timestamp("2025-07-14 20:30", tz="UTC")) == date(2025, 7, 14)
    days = story.recent_trading_days(pd.Timestamp("2025-07-14 10:00", tz="UTC"), 3)
    assert [d.isoformat() for d in days] == ["2025-07-10", "2025-07-11", "2025-07-14"]


def test_break_vs_sweep_labels():
    closes, highs, lows = np.array([10.0, 10.4, 10.2]), np.array([10.3, 10.9, 10.5]), np.array([9.8, 10.1, 9.4])
    assert story.extreme_status(closes, highs, 10.5, "high") == "sweep"       # wick 10.9 > 10.5, no close above
    assert story.extreme_status(closes, highs, 10.3, "high") == "break"       # close 10.4 > 10.3
    assert story.extreme_status(closes, highs, 11.0, "high") == "untouched"
    assert story.extreme_status(closes, lows, 9.5, "low") == "sweep"
    assert story.extreme_status(closes, lows, 10.1, "low") == "break"
    assert story.extreme_status(closes, lows, 9.0, "low") == "untouched"


def test_character_labels():
    base = {"range_pips": 100.0}
    assert story.character({**base, "net_pips": -50, "direction": "down"}, "sweep", "untouched", 0) == \
        "sweep high then reverse"
    assert story.character({**base, "net_pips": 55, "direction": "up"}, "untouched", "sweep", 0) == \
        "sweep low then reverse"
    assert story.character({**base, "net_pips": 40, "direction": "up"}, "break", "untouched", 2) == \
        "breakout up + retest hold"
    assert story.character({**base, "net_pips": -80, "direction": "down"}, "untouched", "untouched", 0) == "trend down"
    assert story.character({**base, "net_pips": 30, "direction": "up"}, "break", "untouched", 0) == "breakout up"
    assert story.character({**base, "net_pips": 5, "direction": "flat"}, "untouched", "untouched", 0) == "range"


def session_frame(start, end, o, c, hi, lo):
    """15m candles drifting linearly from o to c with one wick high / low inside."""
    idx = pd.date_range(start, end, freq="15min", tz="UTC", inclusive="left")
    cl = np.linspace(o, c, len(idx) + 1)[1:]
    op = np.concatenate([[o], cl[:-1]])
    h, l = np.maximum(op, cl) + 0.01, np.minimum(op, cl) - 0.01
    h[len(idx) // 2] = hi
    l[len(idx) // 3] = lo
    return pd.DataFrame({"o": op, "h": h, "l": l, "c": cl, "v": 1.0}, index=idx)


def test_build_story_sessions_break_and_sweep():
    # Tuesday 2025-07-15: Asia ranges 100-101; London wicks above Asia's body high but closes back inside and
    # sells off (sweep high then reverse); New York closes below London's body low (break low, trend down).
    asia = session_frame("2025-07-14 21:00", "2025-07-15 07:00", 100.0, 101.0, 101.2, 99.9)
    ldn = session_frame("2025-07-15 07:00", "2025-07-15 12:00", 101.0, 100.2, 101.6, 100.1)
    ny = session_frame("2025-07-15 12:00", "2025-07-15 21:00", 100.2, 98.0, 100.3, 97.8)
    df = pd.concat([asia, ldn, ny])
    ctx = st.context_from_frame(None, "15m", df, df.index + pd.Timedelta(minutes=15))
    now = pd.Timestamp("2025-07-15 21:00", tz="UTC")
    res = story.build_story(ctx, None, pip=0.1, days=1, now=now)
    rows = {r["session"]: r for r in res["sessions"]}
    assert set(rows) == {"Asia", "London", "New York"}
    a, l, n = rows["Asia"], rows["London"], rows["New York"]
    assert a["open"] == 100.0 and a["close"] == pytest.approx(101.0) and a["direction"] == "up"
    assert a["range_pips"] == pytest.approx((101.2 - 99.9) / 0.1) and a["net_pips"] == pytest.approx(10.0)
    assert l["vs_prev_session"]["high"] == "sweep" and l["vs_prev_session"]["low"] == "untouched"
    assert l["character"] == "sweep high then reverse" and l["direction"] == "down"
    assert n["vs_prev_session"]["low"] == "break" and n["vs_prev_session"]["session_closed_beyond"] == "below"
    assert n["character"] in ("trend down", "breakout down", "breakout down + retest hold")
    assert l["high_time"] == "2025-07-15T09:30:00Z" and not n["in_progress"]
    nowst = res["now"]
    assert nowst["today"]["wick_high"] == pytest.approx(101.6) and nowst["today"]["wick_low"] == pytest.approx(97.8)
    # 21:00 UTC = 17:00 New York: the next trading day's Asia block starts exactly now
    assert nowst["next_actions"] and nowst["current_session"] == "Asia" and nowst["next_session"]["name"] == "London"
    highs = [u for u in nowst["untouched_session_extremes"] if u["side"] == "high"]
    assert any(u["session"].startswith("London") and u["body"] == pytest.approx(l["body_high"]) for u in highs)


# ------------------------------------------------------------------ replay: no candle after as_of matters

def walk15(n=3000, seed=4):
    rng = np.random.default_rng(seed)
    c = 150 + np.cumsum(rng.normal(0, 0.05, n))
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2025-06-02 00:00", periods=n, freq="15min", tz="UTC")   # Monday
    df = pd.DataFrame({"o": o, "h": np.maximum(o, c) + rng.uniform(0, 0.04, n),
                       "l": np.minimum(o, c) - rng.uniform(0, 0.04, n), "c": c, "v": 1.0}, index=idx)
    return data.drop_fx_closed(df)


def feed(df15):
    h1 = resample_ohlc(df15, "1h")
    frames = {"15m": df15, "1h": h1, "4h": resample_ohlc(h1, "4h"), "1d": fx_daily_from_hourly(h1)}
    frames["1wk"] = resample_ohlc(frames["1d"], "W-MON")

    def live(sym, interval):
        return data.Series(df=frames[interval], source="synthetic", ticker="TEST", interval=interval,
                           delayed_minutes=0)
    return live


@pytest.fixture(autouse=True)
def _clean():
    cache.clear()
    yield
    cache.clear()


T = pd.Timestamp("2025-06-24 14:00", tz="UTC")


def _strip(d):
    return {k: v for k, v in d.items() if k not in ("forming_candle",)}


def test_replay_structure_ignores_candles_after_as_of(monkeypatch):
    full = walk15()
    assert full.index[-1] > T + pd.Timedelta(days=5)
    monkeypatch.setattr(data, "_live_series", feed(full))
    with replay(T):
        a = st.analyze(SYM, "1h")
        a15 = st.analyze(SYM, "15m")
        sig_a = st.signals(SYM, ["4h", "1h"])
    # the same moment "live": the feed simply has nothing after T yet
    monkeypatch.setattr(data, "_live_series", feed(full[full.index + pd.Timedelta(minutes=15) <= T]))
    cache.clear()
    b = st.analyze(SYM, "1h", now=T)
    b15 = st.analyze(SYM, "15m", now=T)
    sig_b = st.signals(SYM, ["4h", "1h"], now=T)
    assert _strip(a) == _strip(b) and _strip(a15) == _strip(b15)
    assert a["last_closed_candle_close_utc"] == "2025-06-24T14:00:00Z" and a["forming_candle"] is None
    for iv in ("4h", "1h"):
        x, y = sig_a["intervals"][iv], sig_b["intervals"][iv]
        assert x["last_closed_candle"] == y["last_closed_candle"] and x["trend"] == y["trend"]
        assert [(s["id"], s.get("stop")) for s in x["signals"]] == [(s["id"], s.get("stop")) for s in y["signals"]]
    # and changing the future does not change the replay
    pert = full.copy()
    pert.loc[pert.index >= T, ["o", "h", "l", "c"]] += 7.0
    monkeypatch.setattr(data, "_live_series", feed(pert))
    cache.clear()
    with replay(T):
        assert _strip(st.analyze(SYM, "1h")) == _strip(a)


def test_replay_session_story_ignores_candles_after_as_of(monkeypatch):
    monkeypatch.setattr(story, "get_calendar", lambda *a, **k: {"events": [], "notes": []})
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    with replay(T):
        a = story.session_story(SYM, "15m", days=3, pip=0.01)
    monkeypatch.setattr(data, "_live_series", feed(full[full.index + pd.Timedelta(minutes=15) <= T]))
    cache.clear()
    with replay(T):   # same clock, but the data source itself ends at T
        b = story.session_story(SYM, "15m", days=3, pip=0.01)
    assert a["sessions"] == b["sessions"] and a["now"] == b["now"] and a["markdown"] == b["markdown"]
    assert a["now"]["now_utc"] == "2025-06-24T14:00:00Z" and a["now"]["current_session"] == "New York"
    assert a["now"]["minutes_into_session"] == 120                      # NY opened 12:00 UTC (EDT)
    assert all(pd.Timestamp(r["start_utc"]) < T for r in a["sessions"])
    assert a["sessions"][-1]["in_progress"] and len(a["sessions"]) == 9
    assert max(pd.Timestamp(r["high_time"]) for r in a["sessions"]) < T


def test_price_and_truncation_in_replay(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    with replay(T):
        p = data.get_price(SYM)
        s = data.get_series(SYM, "4h")
        d = data.get_series(SYM, "1d")
    last15 = full[full.index + pd.Timedelta(minutes=15) <= T]
    assert p["price"] == pytest.approx(float(last15["c"].iloc[-1])) and p["as_of"] == "2025-06-24T14:00:00Z"
    assert (s.df.index + pd.Timedelta(hours=4) <= T).all()
    assert d.df.index[-1] == pd.Timestamp("2025-06-23", tz="UTC")       # 06-24's bar closes 21:00 UTC > T
    assert data.replay_as_of() is None                                   # context is restored
    with pytest.raises(ValueError):
        data.parse_as_of("2999-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        data.parse_as_of("not a date")


def test_http_replay_and_ohlc_range(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main, symbols
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    monkeypatch.setattr(main, "normalize", lambda s: SYM)
    c = TestClient(main.app)
    r = c.get("/structure", params={"symbol": "x", "interval": "1h", "as_of": "2025-06-24T14:00:00Z"}).json()
    assert r["replay"] is True and r["replay_as_of"] == "2025-06-24T14:00:00Z"
    assert r["last_closed_candle_close_utc"] == "2025-06-24T14:00:00Z"
    r = c.get("/price", params={"symbol": "x", "as_of": "2025-06-24T14:00:00Z"}).json()
    assert r["replay"] and r["as_of"] == "2025-06-24T14:00:00Z"
    r = c.get("/ohlc", params={"symbol": "x", "interval": "15m", "start": "2025-06-24T14:00:00Z",
                               "end": "2025-06-24T16:00:00Z"}).json()
    assert r["count"] == 9 and r["candles"][0]["t"] == "2025-06-24T14:00:00Z"
    assert r["candles"][-1]["t"] == "2025-06-24T16:00:00Z" and "replay" not in r
    assert c.get("/structure", params={"symbol": "x", "as_of": "garbage"}).status_code == 400
    assert c.get("/structure", params={"symbol": "x", "as_of": "2020-01-01T00:00:00Z"}).status_code == 404
