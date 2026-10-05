import json

import numpy as np
import pandas as pd
import pytest

from app import chart as C
from app import data, mtf
from app import events as EV
from app import patterns as PT
from app.cache import cache
from app.data import replay
from app.snapshot import jsonable

from .test_mtf import BASE, N0, ctx_of, from_closes, stack_ctxs
from .test_story_replay import SYM, feed, walk15

T = pd.Timestamp("2025-06-24 14:20", tz="UTC")           # last closed 15m candle: 14:00 -> 14:15
TABLE = {"events": [
    {"time_utc": "2025-06-23T12:30:00Z", "currency": "USD", "type": "cpi", "title": "US CPI", "source": "t",
     "scheduled": True, "approximate": False},
    {"time_utc": "2025-06-24T15:00:00Z", "currency": "USD", "type": "fomc", "title": "FOMC statement", "source": "t",
     "scheduled": True, "approximate": False},
    {"time_utc": "2025-06-24T16:00:00Z", "currency": "USD", "type": "central_bank", "title": "emergency cut",
     "source": "t", "scheduled": False, "approximate": False},          # not known before it happens
    {"time_utc": "2025-06-24T13:00:00Z", "currency": "EUR", "type": "central_bank", "title": "ECB", "source": "t",
     "scheduled": True, "approximate": False},                          # other currency
]}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    cache.clear()
    monkeypatch.setattr(EV, "load_table", lambda: TABLE)
    monkeypatch.setattr(C, "_odds_for", lambda *a, **k: ({}, False))    # no background threads in tests
    yield
    cache.clear()


def served(full, at=T, bars=200, **kw):
    """The endpoint's payload exactly as it is sent (through the JSON rounding)."""
    with replay(at):
        return json.loads(json.dumps(jsonable(C.chart(SYM, "15m", bars, pip=0.01, **kw))))


def all_times(d):
    out = []
    out += [("swing.t", s["t"]) for s in d["swings"]] + [("swing.confirmed_at", s["confirmed_at"]) for s in d["swings"]]
    for b in d["boxes"]:
        out += [(f"box.{k}", b.get(k)) for k in ("start", "end", "break_time", "known_at")]
    out += [("event.t", e["t"]) for e in d["events"]]
    for i in d["impulses"]:
        out += [("impulse.start", i["start"]), ("impulse.end", i["end"])]
    for p in d["patterns"]:
        out += [("pattern.point", q["t"]) for q in p["points"]]
        out += [("pattern.line", q["t"]) for q in p.get("trigger_line", [])]
        out += [("pattern.known_at", p.get("known_at")), ("pattern.confirmed_at", p.get("confirmed_at"))]
    for s in d["sessions"]:
        out += [("session.start", s["start"]), ("session.end", s["end"])]
    out += [("news.t", x["t"]) for x in d["news"]]
    return [(k, v) for k, v in out if v is not None]


def test_contract_and_every_coordinate_is_a_candle_time(monkeypatch):
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    d = served(None)
    assert list(d)[:6] == ["symbol", "interval", "pip", "as_of", "price", "data_note"]
    assert set(d) >= {"candles", "swings", "boxes", "zones", "events", "impulses", "patterns", "sessions", "news",
                      "playbook", "higher", "reading"}
    assert isinstance(d["data_note"], str) and d["params"]["timeframes"] == ["4h", "1h", "15m"]
    cs = d["candles"]
    assert len(cs) == 200 and set(cs[0]) == {"t", "o", "h", "l", "c"} and not any(c.get("forming") for c in cs)
    times = [c["t"] for c in cs]
    assert times == sorted(times) and times[-1] == "2025-06-24T14:00:00Z" and d["last_closed"] == "2025-06-24T14:15:00Z"
    known, first, last = set(times), times[0], times[-1]
    rows = all_times(d)
    assert len(rows) > 100
    off = [(k, v) for k, v in rows if v not in known and first <= v <= last]
    assert off == []                                    # on a candle, or outside the window
    # what may lie outside: only starts before the window (flagged) and session ends / news still to come
    before = {k for k, v in rows if v < first}
    assert before <= {"impulse.start", "pattern.point", "pattern.line", "pattern.known_at", "box.start", "box.end",
                      "box.break_time"}
    assert {k for k, v in rows if v > last} <= {"session.end", "news.t"}
    for b in d["boxes"]:
        assert b["top"] > b["bottom"] and b["wick_high"] >= b["top"] and b["wick_low"] <= b["bottom"]
        assert b["state"] in ("active", "broken_up", "broken_down") and (b["end"] is None) == (b["state"] == "active")
        if b.get("started_before") and not b.get("ended_before"):
            assert b["start"] == first                  # clamped to the first candle
        if b["interval"] == "15m" and not b.get("started_before"):
            assert b["start"] <= b["known_at"]
    assert {b["interval"] for b in d["boxes"]} <= {"15m", "1h", "4h"} and any(b["interval"] == "15m" for b in d["boxes"])
    # prices of the drawings are prices of the candles they sit on
    by_t = {c["t"]: c for c in cs}
    for s in d["swings"]:
        c = by_t[s["t"]]
        assert s["price"] == (max(c["o"], c["c"]) if s["kind"] == "high" else min(c["o"], c["c"]))   # BODY swing
        assert s["label"] in ("HH", "HL", "LH", "LL", "H", "L") and s["confirmed_at"] > s["t"]
    for e in d["events"]:
        assert e["type"] in ("sweep_high", "sweep_low", "fakeout_up", "fakeout_down", "break_up", "break_down",
                             "retest_hold", "retest_fail") and e["text"] and e["of"] in ("swing", "box", "zone")
        c = by_t[e["t"]]
        if e["type"].startswith("sweep"):
            assert e["price"] == (c["h"] if e["type"] == "sweep_high" else c["l"])
        elif e["of"] != "zone":
            assert e["price"] == c["c"]
    lo, hi = min(c["l"] for c in cs), max(c["h"] for c in cs)
    assert d["zones"] == sorted(d["zones"], key=lambda z: -z["level"])
    assert sum(z["outside_view"] for z in d["zones"]) <= 2
    for z in d["zones"]:
        assert z["low"] < z["level"] < z["high"] and z["role"] == ("support" if d["last_close"] > z["level"] else "resistance")
        assert z["outside_view"] == (not lo <= z["level"] <= hi) and z["touches"] >= z["respected"] >= 0
        assert set(z["timeframes"]) <= {"4h", "1h", "15m"} and z["timeframes"]
    for i in d["impulses"]:
        assert i["direction"] == ("up" if i["to"] > i["from"] else "down") and i["pips"] == pytest.approx(abs(i["to"] - i["from"]) / 0.01, abs=0.05)
    assert len(json.dumps(d)) < 400_000


def test_swings_boxes_and_playbook_are_the_mtf_ones(monkeypatch):
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    d = served(None)
    with replay(T):
        m = jsonable(mtf.mtf(SYM, ["4h", "1h", "15m"], pip=0.01, sl_pips=20))
    lab = {"EQH": "H", "EQL": "L"}
    want = [(s["time"], lab.get(s["label"], s["label"]), s["price"]) for s in m["timeframes"]["15m"]["swings"]]
    got = [(s["t"], s["label"], s["price"]) for s in d["swings"]][-len(want):]
    assert got == want and len(want) == 6
    assert d["reading"]["regime"] == m["timeframes"]["15m"]["regime"]
    for iv in ("1h", "4h"):
        tf, h = m["timeframes"][iv], d["higher"][iv]
        assert (h["regime"], h["swing_trend"]) == (tf["regime"], tf["swing_trend"])
        assert (h["box"] is None) == (tf["box"] is None)
        if tf["box"]:
            assert (h["box"]["top"], h["box"]["bottom"]) == (tf["box"]["top"], tf["box"]["bottom"])
            drawn = [b for b in d["boxes"] if b["interval"] == iv]
            assert len(drawn) == 1 and (drawn[0]["top"], drawn[0]["bottom"]) == (tf["box"]["top"], tf["box"]["bottom"])
        assert h["pullback"] is None or (h["pullback"]["dir"] == tf["swing_trend"] and 0 <= h["pullback"]["depth"] <= 1.5)
    pb, mp = d["playbook"], m["playbook"]
    assert pb["mode"] == mp["mode"] and len(pb["cases"]) == len(mp["cases"])
    for a, b in zip(pb["cases"], mp["cases"]):
        assert (a["action"], a["when"], a["entry_price"], a["stop"]) == (b["action"], b["when"], b["entry_price"], b["stop"])
        assert a["targets"] == [t["price"] for t in b["targets"]] and all(isinstance(x, float) for x in a["targets"])
    assert set(pb) >= {"mode", "direction", "zone", "sell_zone", "buy_zone", "cancel", "cases"} and pb["cancel"]
    if pb["mode"] == "range":
        assert pb["zone"] is None and pb["sell_zone"]["low"] < pb["sell_zone"]["high"] and pb["buy_zone"]["targets"]
    r = d["reading"]
    assert r["phase"] in C.PHASES and r["summary"].count(". ") >= 1 and len(r["what_next"]) >= 2 and r["wait_for"]
    assert r["risk"]["rule"] == C.RISK_RULE and r["risk"]["level"] in ("low", "medium", "high")


def test_as_of_shows_nothing_from_the_future(monkeypatch):
    full = walk15()
    monkeypatch.setattr(data, "_live_series", feed(full))
    a = served(None)
    assert a["replay"] if "replay" in a else True
    # the same call when the feed really ends at T gives the same chart: nothing after T was used
    monkeypatch.setattr(data, "_live_series", feed(full[full.index + pd.Timedelta(minutes=15) <= T]))
    cache.clear()
    b = served(None)
    assert a == b
    last = a["candles"][-1]["t"]
    assert a["as_of"] == "2025-06-24T14:20:00Z" and a["price"] == a["candles"][-1]["c"] == a["last_close"]
    assert max(v for k, v in all_times(a) if k not in ("session.end", "news.t")) <= last
    # an earlier moment is a prefix: same candles up to there, and its drawings never mention a later candle
    early = served(None, at=pd.Timestamp("2025-06-23 09:05", tz="UTC"))
    assert early["candles"][-1]["t"] == "2025-06-23T08:45:00Z"
    assert early["candles"][-50:] == [c for c in a["candles"] if c["t"] <= "2025-06-23T08:45:00Z"][-50:]
    assert max(v for k, v in all_times(early) if k not in ("session.end", "news.t")) <= "2025-06-23T08:45:00Z"
    # news: scheduled events of the pair's currency only; an unscheduled one is not shown ahead of time
    news = {x["title"]: x for x in a["news"]}
    assert set(news) == {"US CPI", "FOMC statement"}
    assert news["US CPI"]["past"] and news["US CPI"]["t"] == "2025-06-23T12:30:00Z"
    assert not news["FOMC statement"]["past"] and news["FOMC statement"]["minutes"] == 40
    assert news["FOMC statement"]["t"] == "2025-06-24T15:00:00Z" > last
    assert a["reading"]["risk"]["factors"]["news_within_60m"] and "FOMC statement" in a["reading"]["wait_for"][0]
    assert not early["reading"]["risk"]["factors"]["news_within_60m"]
    # sessions overlap the window; the one still running ends after the last candle
    assert {s["name"] for s in a["sessions"]} == {"Sydney", "Tokyo", "London", "New York"}
    live = [s for s in a["sessions"] if s.get("in_progress")]
    assert {s["name"] for s in live} == {"London", "New York"} and all(s["end"] > last for s in live)


def test_polls_between_closes_reuse_the_closed_candle_work(monkeypatch):
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    calls = []
    real = C.build_chart
    monkeypatch.setattr(C, "build_chart", lambda *a, **k: calls.append(1) or real(*a, **k))
    a = served(None)
    b = served(None, at=T + pd.Timedelta(minutes=8))        # same closed candle, later clock
    assert len(calls) == 1 and a["candles"] == b["candles"] and a["swings"] == b["swings"]
    assert b["as_of"] == "2025-06-24T14:28:00Z" and {x["minutes"] for x in b["news"] if not x["past"]} == {32}
    served(None, at=T + pd.Timedelta(minutes=12))            # 14:32: the 14:15 candle has closed
    assert len(calls) == 2


def risk(**kw):
    base = {"stop": 0.0020, "pip": 0.0001, "atr": 0.0010, "interval": "15m", "news_minutes": None, "box_where": None,
            "entry_dir": 0, "htf_dir": 0, "fakeouts": 0, "sessions": ["London"], "asia_open": False,
            "market_open": True, "data_age_candles": 0.4}
    return C.assess_risk({**base, **kw})


def test_risk_rule_on_constructed_cases():
    r = risk()
    assert (r["level"], r["count"], r["reasons"]) == ("low", 0, []) and not any(r["factors"].values())
    # each factor on its own raises low -> medium and gives exactly one reason
    singles = {
        "stop_inside_noise": {"atr": 0.0050},                         # 20-pip stop < half of a 50-pip ATR
        "news_within_60m": {"news_minutes": 45, "news_title": "USD CPI"},
        "mid_box": {"box_where": "mid_box", "box_name": "the 1h box"},
        "against_higher_timeframe": {"entry_dir": -1, "htf_dir": 1},
        "fakeouts_on_box": {"fakeouts": 2},
        "thin_session": {"sessions": ["Sydney"]},
        "closed_or_stale": {"data_age_candles": 2.5},
    }
    for name, kw in singles.items():
        r = risk(**kw)
        assert r["level"] == "medium" and r["count"] == 1 and len(r["reasons"]) == 1, name
        assert [k for k, v in r["factors"].items() if v] == [name]
    assert set(singles) == set(risk()["factors"])
    # boundaries
    assert not risk(atr=0.0040)["factors"]["stop_inside_noise"]        # stop exactly half the ATR is not below it
    assert risk(news_minutes=-60)["factors"]["news_within_60m"] and not risk(news_minutes=61)["factors"]["news_within_60m"]
    assert not risk(box_where="near_top")["factors"]["mid_box"]
    assert not risk(entry_dir=1, htf_dir=1)["factors"]["against_higher_timeframe"]
    assert not risk(entry_dir=0, htf_dir=1)["factors"]["against_higher_timeframe"]     # no entry -> nothing to oppose
    assert not risk(fakeouts=1)["factors"]["fakeouts_on_box"]
    assert not risk(sessions=["Tokyo"], asia_open=True)["factors"]["thin_session"]      # the Asia open is not thin
    assert risk(sessions=["Tokyo"], asia_open=False)["factors"]["thin_session"]
    assert risk(market_open=False)["factors"]["closed_or_stale"] and not risk(data_age_candles=2.0)["factors"]["closed_or_stale"]
    # two factors stay medium, three or more are high
    two = risk(atr=0.0050, fakeouts=3)
    assert (two["level"], two["count"]) == ("medium", 2)
    three = risk(atr=0.0050, fakeouts=3, news_minutes=10, news_title="USD NFP")
    assert (three["level"], three["count"]) == ("high", 3) and "10 min" in three["reasons"][1]
    weekend = risk(market_open=False, sessions=[], box_where="mid_box")
    assert weekend["level"] == "high" and "market is closed" in weekend["reasons"][-1]
    assert "Three" not in C.RISK_RULE and "3 or more = high" in C.RISK_RULE and three["rule"] == C.RISK_RULE


def test_weekend_is_not_counted_as_stale_data():
    ts = lambda s: pd.Timestamp(s, tz="UTC")
    assert C.trading_gap(ts("2026-10-02 21:00"), ts("2026-10-05 01:45")) == pd.Timedelta(hours=4, minutes=45)
    assert C.trading_gap(ts("2026-10-01 10:00"), ts("2026-10-01 12:30")) == pd.Timedelta(hours=2, minutes=30)
    assert C.trading_gap(ts("2026-10-03 10:00"), ts("2026-10-04 10:00")) == pd.Timedelta(0)     # inside the weekend
    assert C._asia_open(ts("2026-10-05 00:30")) and not C._asia_open(ts("2026-10-05 02:30"))    # Tokyo 09:00-11:00


def test_box_drawings_on_a_constructed_range():
    # box 183-187 after an impulse down; a wick sweep of the top, a 2-candle fakeout below, then a real break down
    tail = [181.5] + [180.5 - 0.1 * i for i in range(mtf.MAX_BROKEN + 2)]
    closes = BASE + [185.5, 182.0, 182.4, 184.5, 185.5] + tail
    ctx = ctx_of(from_closes(closes, wicks={N0: {"h": 188.6}}))
    st = mtf.box_states(ctx)
    idx = ctx.df.index
    boxes = C.chart_boxes(ctx, st, 0)
    b = boxes[0]
    assert (b["interval"], b["state"], b["bottom"], b["fakeouts_below"], b["fakeouts_above"]) == ("1h", "broken_down", 183.0, 1, 0)
    brk = N0 + 5
    assert b["end"] == b["break_time"] == C.iso(idx[brk]) and "pending" not in b and "started_before" not in b
    assert (b["first_top"], b["first_bottom"]) == (187.0, 181.0) and b["wick_high"] == 188.6
    assert b["start"] == C.iso(idx[st.episodes[0]["first_box_start"]]) < b["known_at"]
    ev = C.chart_events(ctx, st, 0, 0.1, [], 0, 0, 1e9)
    got = {(e["type"], e["t"]): e for e in ev if e["of"] == "box"}
    sweep = got[("sweep_high", C.iso(idx[N0]))]
    assert sweep["price"] == 188.6 and "21 pips above the box top 186.500" in sweep["text"]
    fake = got[("fakeout_down", C.iso(idx[N0 + 1]))]
    assert fake["price"] == 182.0 and "back inside 2 candle(s) later" in fake["text"]
    real = got[("break_down", C.iso(idx[brk]))]
    assert real["price"] == 181.5 and real["text"].endswith("stayed outside: break")
    # in a window that starts inside the box, the box is clamped and flagged, and earlier events are gone
    w0 = N0 + 3
    clamped = C.chart_boxes(ctx, st, w0)[0]
    assert clamped["start"] == C.iso(idx[w0]) and clamped["started_before"] is True
    assert all(e["t"] >= C.iso(idx[w0]) for e in C.chart_events(ctx, st, w0, 0.1, [], 0, 0, 1e9))
    # the lead-in impulse is drawn from its first big candle to the low
    imp = [i for i in C.chart_impulses(ctx, 0, 0.1) if i["direction"] == "down"][0]
    assert imp["to"] == 181.0 and imp["end"] == C.iso(idx[24]) and imp["pips"] > 190 and imp["start"] < imp["end"]
    # a close outside that is younger than FAKEOUT_MAX candles is marked as not yet decided, and is waited for
    ctx2 = ctx_of(from_closes(BASE + [188.5]))
    st2 = mtf.box_states(ctx2)
    e2 = [e for e in C.chart_events(ctx2, st2, 0, 0.1, [], 0, 0, 1e9) if e["of"] == "box"][-1]
    assert e2["type"] == "break_up" and e2["pending"] and "too early to call" in e2["text"]
    b2 = C.chart_boxes(ctx2, st2, 0)[-1]
    assert b2["state"] == "broken_up" and b2["pending"] and b2["end"] == C.iso(ctx2.df.index[-1])


def test_reading_phase_wait_and_risk_inputs_from_the_plan():
    # 1h range 183-187, price mid-box: nothing to do, and the rule says so
    ctxs = stack_ctxs([185.2])
    s = C.build_chart(ctxs, "15m", 100, pip=0.1, sl_pips=20, now=ctxs["15m"].closes[-1])
    assert s["playbook"]["mode"] == "range" and s["playbook"]["price_position"] == "mid_box"
    assert s["_risk"]["box_where"] == "mid_box" and s["_risk"]["entry_dir"] == 0
    assert "mid-box there is nothing to do" in s["reading"]["wait_for"][0] and s["reading"]["phase"] in C.PHASES
    assert "price is mid-range now" in s["reading"]["summary"]
    out = C.finish(s, ctxs["15m"].closes[-1] + pd.Timedelta(minutes=3))
    assert out["reading"]["risk"]["factors"]["mid_box"] and "no entry" in out["reading"]["risk"]["note"]
    # price at the range top: phase range_edge, the entry the plan points to is a sell, and the sell trigger leads
    top = stack_ctxs([186.4])
    s2 = C.build_chart(top, "15m", 100, pip=0.1, now=top["15m"].closes[-1])
    assert s2["reading"]["phase"] == "range_edge" and s2["_risk"]["entry_dir"] == -1
    assert s2["_risk"]["box_where"] == "near_top" and "CLOSES back below" in s2["reading"]["wait_for"][0]
    # a body close below the range: break awaiting its retest, direction short, zone as two numbers
    brk = stack_ctxs([182.2, 181.9])
    s3 = C.build_chart(brk, "15m", 100, pip=0.1, now=brk["15m"].closes[-1])
    pb = s3["playbook"]
    assert (pb["mode"], pb["direction"], s3["reading"]["phase"]) == ("break_retest", "short", "break_awaiting_retest")
    assert len(pb["zone"]) == 2 and pb["zone"][0] < 183.0 < pb["zone"][1] and "183.000" in pb["cancel"]
    assert s3["_risk"]["entry_dir"] == -1 and s3["higher"]["1h"]["box"]["state"] == "broken_down"
    assert "rejected retest of the broken 1h edge 183.000" in s3["reading"]["summary"]


def test_forming_candle_and_live_price():
    ctxs = stack_ctxs([185.2])
    ctx = ctxs["15m"]
    s = C.build_chart(ctxs, "15m", 60, pip=0.1, now=ctx.closes[-1])
    now = ctx.closes[-1] + pd.Timedelta(minutes=4)
    forming = {"start": C.iso(ctx.closes[-1]), "close_time": C.iso(ctx.closes[-1] + pd.Timedelta(minutes=15)),
               "o": 185.2, "h": 185.5, "l": 185.1, "last": 185.3}
    q = 185.3 + 0.9 * s["atr"]                               # a fresher quote, within one ATR of the candle feed
    out = C.finish(s, now, forming, {"price": q, "source": "quote", "as_of": C.iso(now)})
    f = out["candles"][-1]
    assert len(out["candles"]) == 61 and f["forming"] is True and f["t"] == forming["start"]
    assert (f["c"], f["h"], f["l"]) == (q, max(q, 185.5), 185.1) and (out["price"], out["price_source"]) == (q, "quote")
    assert out["candles"][:-1] == s["candles"] and "forming" not in s["candles"][-1]      # the cached part is untouched
    # a quote that disagrees with the candles by more than one ATR is not used
    far = C.finish(s, now, forming, {"price": 285.0, "source": "quote"})
    assert (far["price"], far["price_source"], far["candles"][-1]["c"]) == (185.3, "forming candle", 185.3)
    none = C.finish(s, now)
    assert none["price"] == s["last_close"] and len(none["candles"]) == 60
    # the feed has not delivered the current candle but a quote exists: a flagged stub that opens at the last close
    q2 = s["last_close"] + 0.5 * s["atr"]
    stub = C.finish(s, now, None, {"price": q2, "source": "quote"})["candles"][-1]
    assert stub == {"t": C.iso(ctx.closes[-1]), "o": s["last_close"], "h": q2, "l": s["last_close"], "c": q2,
                    "forming": True, "synthetic": True, "closes_at": forming["close_time"]}
    late = C.finish(s, now + pd.Timedelta(minutes=30), None, {"price": q2, "source": "quote"})     # a candle is missing
    assert len(late["candles"]) == 60 and late["price"] == q2


def test_patterns_carry_points_and_the_recent_pass_equals_the_full_one():
    rng = np.random.default_rng(11)
    n = 2600
    c = 100 + np.cumsum(rng.normal(0, 0.5, n))
    o = np.concatenate([[c[0]], c[:-1]])
    idx = pd.date_range("2025-03-03", periods=n, freq="h", tz="UTC")
    df = pd.DataFrame({"o": o, "h": np.maximum(o, c) + rng.uniform(0, 0.3, n), "l": np.minimum(o, c) - rng.uniform(0, 0.3, n),
                       "c": c, "v": 1.0}, index=idx)
    ctx = ctx_of(df)
    w0 = n - 300
    full: list = []
    PT.chart_pattern_state(ctx, full)
    a = C.chart_patterns(ctx, w0, full)
    tail, off = C.tail_context(ctx, 300 + C.PATTERN_WARMUP)
    part: list = []
    PT.chart_pattern_state(tail, part)
    b = C.chart_patterns(tail, w0 - off, part)
    assert off == n - 1300 and a == b and len(a) >= 3
    known = set(C.iso(t) for t in idx)
    for p in a:
        assert p["kind"] == "chart" and p["state"] in ("forming", "confirmed") and len(p["points"]) >= 3
        assert all(q["t"] in known for q in p["points"] + p["trigger_line"]) and p["direction"] in ("up", "down", None)
        assert p["points"] == sorted(p["points"], key=lambda q: q["t"]) and p["text"].startswith(p["name"])
        if p["state"] == "forming":
            assert p["alive"] and "wait for a 1h body close" in p["text"]
        else:
            assert p["confirmed_at"] >= p["known_at"] and p["confirmed_at"] >= C.iso(idx[w0])
    # the feature arrays are not changed by logging
    plain = PT.chart_pattern_state(ctx)
    logged = PT.chart_pattern_state(ctx, [])
    assert all(np.array_equal(plain[f][k], logged[f][k], equal_nan=True) for f in plain for k in plain[f])
    # candlestick marks: one point each, on the last 30 candles only
    marks = C.candle_marks(ctx, w0, [], 0)
    assert marks and all(m["kind"] == "candlestick" and len(m["points"]) == 1 for m in marks)
    assert min(m["points"][0]["t"] for m in marks) >= C.iso(idx[n - C.CANDLE_PATTERN_BARS])


def test_chart_endpoint(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main
    monkeypatch.setattr(data, "_live_series", feed(walk15()))
    monkeypatch.setattr(main, "normalize", lambda s: SYM)
    c = TestClient(main.app)
    r = c.get("/chart", params={"symbol": "TESTFX", "interval": "15m", "bars": 120, "pip": 0.01, "as_of": "2025-06-24T14:20:00Z"})
    assert r.status_code == 200
    d = r.json()
    assert d["replay"] is True and len(d["candles"]) == 120 and d["interval"] == "15m" and d["pip"] == 0.01
    assert c.get("/chart", params={"symbol": "TESTFX", "bars": 10}).status_code == 422
    assert c.get("/chart", params={"symbol": "TESTFX", "interval": "30m", "as_of": "2025-06-24T14:20:00Z"}).status_code == 400
