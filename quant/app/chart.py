"""Annotated chart: the candles of ONE interval plus every drawing the house method reads a chart by.

Nothing is detected here. The module only PLACES what the other modules already compute on the candles:
swings and breaks (structure), consolidation boxes / sweeps / fakeouts / impulses / playbook (mtf), support and
resistance zones with their retests (levels), chart and candlestick patterns (patterns), scheduled news
(events + the weekly calendar feed) and session windows (sessions).

Coordinates: every time (`t`, `start`, `end`, `break_time`, `confirmed_at`, `known_at`) is the OPEN time of a
candle in `candles` (the /ohlc convention) or lies outside the window: before the first candle (the row is then
flagged `started_before`, and boxes / sessions are clamped to the first candle) or after the last one (a
session end or a news time that has not happened yet). Exact event times are kept in `time`.

Speed: everything that depends only on CLOSED candles is cached per closed candle; a poll in between only
refreshes the forming candle, `price`, the `past` flag of the news and the time-dependent risk factors.
"""
from __future__ import annotations

import contextvars
import threading
from dataclasses import replace
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from . import events as EV
from . import patterns as PT
from .cache import TTL_MODEL, cache
from .calendar import get_calendar
from .data import DataError, INTERVALS, day_close_mode, get_price, get_series, iso, now_ts, ns_index, replay_as_of
from .levels import LIFETIME_BARS, MAX_CLOSES_THROUGH, build_zones, zone_public, zone_reactions
from .mtf import (BREAK_TOL_ATR, FAKEOUT_MAX, INSIDE, OUT_DOWN, OUT_UP, SWEEP_MIN_ATR, BoxState, _px,
                  analyze_mtf, box_states, build_cases, is_fakeout, is_impulse, legs, make_data_note, tighten_start,
                  timeframe_odds)
from .retest import Level, default_pip
from .sessions import SESSIONS, _occurrences, fx_market_open
from .structure import Context, StructureRun, build_context, closed_frame, is_fx_like, session_tag
from .symbols import Symbol

HIGHER = {"5m": ("15m", "1h"), "15m": ("1h", "4h"), "1h": ("4h", "1d"), "4h": ("1d", "1wk"), "1d": ("1wk", "1mo")}
MAX_CTX_BARS = 60_000  # same cap as /mtf, so the boxes and the playbook are the ones /mtf reports
ZONE_TAIL_MAX = 30_000
PATTERN_WARMUP = 1000  # candles before the window that the chart-pattern pass replays (a pattern lives <= 160)
CANDLE_PATTERN_BARS = 30
ENDED_PATTERNS_MAX = 8
ZONES_MAX = 40
SESSION_MAX_DAYS = 15
NEWS_AHEAD_H = 24
NEWS_RISK_MIN = 60
ASIA_OPEN_HOURS = 2
STATIC_TTL, STATIC_TTL_PENDING = 30 * 60, 45
PHASES = ("consolidation", "impulse", "corrective_retracement", "continuation", "break_awaiting_retest", "range_edge")
RISK_RULE = (
    "Starts at low. One step up for each of: (1) the fixed stop is smaller than half the ATR14 of this interval's "
    "candles; (2) a scheduled high-impact event of the pair's currencies within 60 minutes (before or after); "
    "(3) price is mid-box (between 25% and 75% of the reference box, not at an edge); (4) the entry the plan "
    "points to is against the direction of the higher timeframe; (5) two or more fakeouts on the current box; "
    "(6) thin session: neither London nor New York is open and it is not the first 2 hours of the Tokyo open; "
    "(7) the market is closed or the last closed candle is older than two candles (the FX weekend is not "
    "counted as age). "
    "0 factors = low, 1-2 = medium, 3 or more = high.")
PATTERN_NOTE = ("Patterns describe shape only. In the 10-symbol study (5m / 15m / 1h, 20-pip stop, 50-pip target) "
                "none of them showed a directional edge over shuffled candles; a forming pattern is not a signal "
                "until a body closes through its trigger line, and even then it only says the shape completed.")
_LABEL = {"EQH": "H", "EQL": "L"}
_DIR = {1: "up", -1: "down", 0: None}
_BOX_STATE = {"inside": "active", "closed_above": "broken_up", "closed_below": "broken_down"}
_CS_NAMES = {"cs_engulfing": ("bullish engulfing", "bearish engulfing"), "cs_pin": ("hammer", "shooting star"),
             "cs_inside": ("inside bar", "inside bar"), "cs_inside_break": ("inside-bar break up", "inside-bar break down"),
             "cs_outside": ("outside bar up", "outside bar down"),
             "cs_doji_zone": ("doji at support", "doji at resistance"), "cs_star": ("morning star", "evening star"),
             "cs_three": ("three white soldiers", "three black crows"), "cs_tweezer": ("tweezer bottom", "tweezer top")}


# ------------------------------------------------------------------ drawings (pure; closed candles only)

def chart_candles(ctx: Context, w0: int) -> list[dict]:
    df = ctx.df.iloc[w0:]
    return [{"t": iso(t), "o": float(r.o), "h": float(r.h), "l": float(r.l), "c": float(r.c)}
            for t, r in zip(df.index, df.itertuples(index=False))]


def chart_swings(ctx: Context, w0: int) -> list[dict]:
    """Confirmed BODY swings inside the window (the same list /mtf reads its HH / HL / LH / LL from)."""
    idx = ctx.df.index
    out = []
    for s in ctx.run.swings:
        if s.idx < w0:
            continue
        lab = s.label or s.kind
        row = {"t": iso(idx[s.idx]), "price": s.price, "kind": "high" if s.kind == "H" else "low",
               "label": _LABEL.get(lab, lab), "confirmed_at": iso(idx[s.confirmed_idx]), "wick": s.wick}
        if lab in _LABEL:
            row["equal"] = True  # body equal to the previous swing of its kind: neither higher nor lower
        out.append(row)
    return out


def chart_boxes(ctx: Context, st: BoxState, w0: int) -> list[dict]:
    """Consolidation boxes of this interval that overlap the window. `start` is the first swing of the box
    (clamped to the window), `known_at` the candle whose close made the box known."""
    df = ctx.df
    n = len(df)
    idx = df.index
    h, l = df["h"].to_numpy(float), df["l"].to_numpy(float)
    out = []
    for ep in st.episodes:
        ended = ep["end_idx"] is not None
        last = ep["end_idx"] if ended else n - 1
        if last < w0 or "top" not in ep:
            continue
        s0 = max(int(ep["first_box_start"]), 0)
        brk, state, pending = None, "active", False
        if ended:
            brk, state = int(ep["end_idx"]), f"broken_{ep['resolved']}"
        elif st.status[n - 1] in (OUT_UP, OUT_DOWN) and st.episode[n - 1] == ep["id"]:
            brk, state, pending = int(st.break_idx[n - 1]), "broken_up" if st.status[n - 1] == OUT_UP else "broken_down", True
        w = slice(s0, (brk if brk is not None else n - 1) + 1)
        fo = [b for b in ep["breaks"] if is_fakeout(b)]
        row = {"interval": ctx.interval, "start": iso(idx[max(s0, w0)]), "end": iso(idx[brk]) if brk is not None else None,
               "top": float(ep["top"]), "bottom": float(ep["bot"]), "wick_high": float(h[w].max()),
               "wick_low": float(l[w].min()), "state": state, "break_time": iso(idx[brk]) if brk is not None else None,
               "known_at": iso(idx[max(int(ep["first_idx"]) - 1, w0)]), "first_top": float(ep["outer_top"]),
               "first_bottom": float(ep["outer_bot"]), "fakeouts_above": sum(b["side"] > 0 for b in fo),
               "fakeouts_below": sum(b["side"] < 0 for b in fo)}
        if s0 < w0:
            row["started_before"] = True
        if pending:
            row["pending"] = True  # closed outside less than 24 candles ago: a close back inside revives the box
        out.append(row)
    return out


def higher_box(tf: dict, ctx: Context, w0: int) -> Optional[dict]:
    """The current box of a higher timeframe, placed on this interval's candles."""
    box = tf.get("box")
    if not box:
        return None
    idx = ns_index(ctx.df.index)
    closes = ns_index(ctx.closes)
    t0 = pd.Timestamp(box["start"])
    i0 = int(idx.searchsorted(t0, side="left"))
    p = box["position"]
    row = {"interval": tf["interval"], "start": iso(idx[min(max(i0, w0), len(idx) - 1)]), "end": None,
           "top": box["top"], "bottom": box["bottom"], "wick_high": box["wick_high"], "wick_low": box["wick_low"],
           "state": _BOX_STATE[p["state"]], "break_time": None, "fakeouts_above": box.get("fakeouts_above", 0),
           "fakeouts_below": box.get("fakeouts_below", 0)}
    if i0 < w0:
        row["started_before"] = True
    if p["state"] != "inside":
        j = int(closes.searchsorted(pd.Timestamp(p["break_candle_close_time"]), side="right")) - 1
        if j < w0:  # broken before the first candle of the window: only its edges still matter
            row.update(start=box["start"], end=iso(pd.Timestamp(p["break_candle_time"])),
                       break_time=iso(pd.Timestamp(p["break_candle_time"])), ended_before=True)
        else:
            row.update(end=iso(idx[j]), break_time=iso(idx[j]))
        row["pending"] = True
    return row


def chart_zones(ctxs: dict[str, Context], order: list[str], base_iv: str, w0: int, pip: float,
                now: pd.Timestamp) -> tuple[list[dict], list[dict], int]:
    """Zones from the swing levels of every interval, reactions walked on this interval's candles (the /levels
    engine on the part of the history that can still matter: one zone lifetime before the window).
    Returns (all zones, zones alive now, index of the first candle that was walked)."""
    base = ctxs[base_iv]
    closes = ns_index(base.closes)
    n = len(closes)
    t_w0 = base.df.index[w0]
    earliest = t_w0
    levels = []
    for iv in order:
        cut = t_w0 - pd.Timedelta(hours=LIFETIME_BARS * INTERVALS[iv][4] * 1.6)  # 1.6: weekends + holidays
        earliest = min(earliest, cut)
        cx = ctxs[iv]
        ci = int(ns_index(cx.closes).searchsorted(cut, side="left"))  # = levels_from_context, recent swings only
        levels += [Level(float(s.price), s.kind, s.label or s.kind, cx.df.index[s.idx], cx.closes[s.confirmed_idx], iv)
                   for s in cx.run.history if s.confirmed_idx >= ci]
    b0 = max(int(closes.searchsorted(earliest, side="right")) - 1, 0, n - ZONE_TAIL_MAX)
    sl = slice(b0, n)
    o, h, l, c = (base.df[x].to_numpy(float)[sl] for x in ("o", "h", "l", "c"))
    arr = (o, h, l, c, base.atr_prev[sl], base.imp[sl], pip)
    zones = build_zones(levels, closes[sl], base.atr[sl], base_iv, arrays=arr)
    for z in zones:
        z["reactions"] = zone_reactions(z, o, h, l, c, closes[sl], base.atr_prev[sl], base.imp[sl], pip, resume=True)
    m = n - b0
    alive = [z for z in zones if z["expires_pos"] >= m and z.get("closes_through", 0) < MAX_CLOSES_THROUGH
             and z["known_time"] <= now]
    return zones, alive, b0


def visible_zones(alive: list[dict], lo: float, hi: float, last: float, pip: float, now: pd.Timestamp,
                  order: list[str]) -> list[dict]:
    """Zones inside the visible price range, plus the nearest one above it and the nearest one below it."""
    inside = [z for z in alive if lo <= z["price"] <= hi]
    above = min((z for z in alive if z["price"] > hi), key=lambda z: z["price"], default=None)
    below = max((z for z in alive if z["price"] < lo), key=lambda z: z["price"], default=None)
    rows = []
    for z in inside + [x for x in (above, below) if x is not None]:
        p = zone_public(z, last, pip, now, order)
        rs = [r for r in z.get("reactions", []) if r["type"] != "in_progress"]
        rows.append({"id": z["id"], "level": p["level"], "low": p["low"], "high": p["high"], "role": p["role"],
                     "timeframes": p["timeframes"], "touches": len(rs), "respected": p["respected_touches"],
                     "flips": p["flips"], "strength": p["score"], "closes_through": p["closes_through"],
                     "awaiting_retest": p["awaiting_retest"], "first_seen": p["first_seen"],
                     "outside_view": not lo <= p["level"] <= hi})
    if len(rows) > ZONES_MAX:  # keep the strongest; the nearest line on each side always stays
        keep = sorted(rows, key=lambda r: (not r["outside_view"], -r["strength"]))
        rows = [r for r in keep if r["outside_view"]] + [r for r in keep if not r["outside_view"]][:ZONES_MAX]
    return sorted(rows, key=lambda r: -r["level"])


def chart_events(ctx: Context, st: BoxState, w0: int, pip: float, zones: list[dict], b0: int, lo: float,
                 hi: float, shown: Optional[set] = None) -> list[dict]:
    """Sweeps, fakeouts, breaks and retests inside the window, one marker per candle and type."""
    df = ctx.df
    n = len(df)
    idx = df.index
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    iv = ctx.interval
    shown = {z["id"] for z in zones} if shown is None else shown
    ev: dict = {}

    def put(i: int, typ: str, price: float, text: str, of: str, prio: int, **extra) -> None:
        k = (i, typ)
        if k in ev and ev[k]["_p"] >= prio:
            return
        ev[k] = {"t": iso(idx[i]), "price": float(price), "type": typ, "text": text, "of": of, "_p": prio, "_i": i,
                 **extra}

    def atr_before(t: int) -> float:
        a = ctx.atr[t - 1] if t > 0 else np.nan
        return float(a) if np.isfinite(a) else float(ctx.atr[t]) if np.isfinite(ctx.atr[t]) else 0.0
    # wicks beyond a confirmed swing that closed back (the largest one per swing), and body closes beyond it
    best: dict = {}
    for e in ctx.run.events:
        if e.idx < w0:
            continue
        up = e.type.endswith(("high", "up"))
        lab = e.swing_label or ("H" if up else "L")
        if e.type.startswith("sweep"):
            if abs(e.wick - e.level) < SWEEP_MIN_ATR * atr_before(e.idx):
                continue
            k = (e.swing_idx, e.type)
            if k not in best or (e.wick > best[k].wick if up else e.wick < best[k].wick):
                best[k] = e
        elif e.idx > 0 and ctx.run.trend[e.idx - 1] == ("down" if up else "up"):
            # only closes AGAINST the swing trend: the protected swing of that trend is broken
            put(e.idx, "break_up" if up else "break_down", e.close,
                f"{iv} body closed {'above' if up else 'below'} the {'lower high' if up else 'higher low'} "
                f"{_px(e.level)} (close {_px(e.close)}) while the swing trend was {'down' if up else 'up'}: "
                f"that trend's protected swing is broken by close", "swing", 0)
    for e in best.values():
        up = e.type == "sweep_high"
        put(e.idx, e.type, e.wick,
            f"wick {abs(e.wick - e.level) / pip:.0f} pips {'above' if up else 'below'} the swing "
            f"{'high' if up else 'low'} {e.swing_label or ''} {_px(e.level)}, body closed back "
            f"{'below' if up else 'above'} it ({_px(e.close)}): sweep, not a break".replace("  ", " "), "swing", 1)
    # wicks beyond the edges of the box the candle was judged against (drawn from swings confirmed before it)
    for t in range(max(w0, 1), n):
        if st.status[t] != INSIDE:
            continue
        a = atr_before(t)
        if a <= 0:
            continue
        top, bot, tol = float(st.top[t]), float(st.bot[t]), BREAK_TOL_ATR * a
        if h[t] - top >= SWEEP_MIN_ATR * a and c[t] <= top + tol:
            put(t, "sweep_high", h[t], f"wick {(h[t] - top) / pip:.0f} pips above the box top {_px(top)}, body closed "
                                       f"back inside ({_px(c[t])}): sweep, not a break", "box", 2)
        if bot - l[t] >= SWEEP_MIN_ATR * a and c[t] >= bot - tol:
            put(t, "sweep_low", l[t], f"wick {(bot - l[t]) / pip:.0f} pips below the box bottom {_px(bot)}, body "
                                      f"closed back inside ({_px(c[t])}): sweep, not a break", "box", 2)
    # body closes outside a box: fakeout (back inside within FAKEOUT_MAX candles) or break
    for b in st.breaks:
        t = b["idx"]
        if t < w0:
            continue
        up = b["side"] > 0
        d, word, name = ("up", "above", "top") if up else ("down", "below", "bottom")
        head = f"{iv} body closed {word} the box {name} {_px(b['edge'])} (close {_px(b['close'])})"
        if b["back_idx"] is None:
            if t + FAKEOUT_MAX >= n:
                put(t, f"break_{d}", b["close"], f"{head}; too early to call: a close back inside within {FAKEOUT_MAX} "
                                                f"candles makes it a fakeout", "box", 3, pending=True)
            else:
                put(t, f"break_{d}", b["close"], f"{head} and price stayed outside: break", "box", 3)
        elif is_fakeout(b):
            put(t, f"fakeout_{d}", b["close"], f"{head} but a body was back inside {b['back_idx'] - t} candle(s) "
                                              f"later: fakeout", "box", 3)
        else:
            put(t, f"break_{d}", b["close"], f"{head}; a body closed back inside {b['back_idx'] - t} candles later "
                                            f"(failed break)", "box", 3, failed=True)
    # first touch of a DRAWN zone after a body close through it
    for z in zones:
        P = z["price"]
        if not lo <= P <= hi or z["id"] not in shown:
            continue
        for r in z.get("reactions", []):
            i = b0 + r["idx"]
            if i < w0 or r["type"] not in ("retest_hold", "retest_fail"):
                continue
            role = r["role_tested"]
            tfs = "+".join(r["confluence"]) or iv
            text = (f"retest of {_px(P)} ({tfs}) held as {role} after the break" if r["type"] == "retest_hold" else
                    f"retest of {_px(P)} ({tfs}) failed: a body closed back through it")
            put(i, r["type"], P, text, "zone", 1)
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in sorted(ev.values(), key=lambda e: e["_i"])]


def chart_impulses(ctx: Context, w0: int, pip: float) -> list[dict]:
    df = ctx.df
    idx = df.index
    o, c = df["o"].to_numpy(float), df["c"].to_numpy(float)
    out = []
    for leg in legs(ctx):
        if leg["end_idx"] < w0 or not is_impulse(leg):
            continue
        s = tighten_start(ctx, leg)
        up = leg["move"] > 0
        frm = float(min(o[s], c[s]) if up else max(o[s], c[s])) if s != leg["start_idx"] else float(leg["from"])
        row = {"start": iso(idx[s]), "end": iso(idx[leg["end_idx"]]), "from": frm, "to": float(leg["to"]),
               "direction": "up" if up else "down", "pips": abs(leg["to"] - frm) / pip,
               "candles": int(max(leg["end_idx"] - s, 1)), "in_progress": bool(leg["in_progress"])}
        if s < w0:
            row["started_before"] = True
        out.append(row)
    return out


def tail_context(ctx: Context, keep: int) -> tuple[Context, int]:
    """The last `keep` candles of a context with the swing history re-indexed (for causal passes that only
    need recent history). Returns (context, number of candles dropped)."""
    off = len(ctx.df) - int(keep)
    if off <= 0:
        return ctx, 0
    hist = [replace(s, idx=s.idx - off, confirmed_idx=s.confirmed_idx - off) for s in ctx.run.history if s.idx >= off]
    run = StructureRun([], [], ctx.run.trend[off:], [], hist)
    return Context(ctx.sym, ctx.interval, ctx.source, ctx.delayed_minutes, ctx.df.iloc[off:], ctx.closes[off:],
                   ctx.forming, ctx.atr[off:], ctx.atr_prev[off:], ctx.imp[off:], run, ctx.htf, ctx.htf_trend[off:],
                   ctx.trend_num[off:], ctx.source_info), off


def _pattern_name(p: dict) -> str:
    f, d = p["family"], p["dir"]
    if f in ("double", "triple"):
        return f"{f} {'top' if d < 0 else 'bottom'}"
    if f == "hs":
        return "head and shoulders" if d < 0 else "inverse head and shoulders"
    if f == "triangle":
        return f"{p.get('kind', 'symmetrical')} triangle"
    if f == "wedge":
        return "rising wedge" if d < 0 else "falling wedge"
    return f"{'bull' if d > 0 else 'bear'} {f}"


def chart_patterns(ctx: Context, w0: int, log: list[dict]) -> list[dict]:
    """Chart patterns alive now, plus those confirmed inside the window that have since been dropped."""
    n = len(ctx.df)
    idx = ctx.df.index
    iv = ctx.interval
    alive = [p for p in log if p["end"] is None]
    ended = [p for p in log if p["end"] is not None and p["state"] == 2 and p["conf"] >= w0][-ENDED_PATTERNS_MAX:]
    out = []
    for p in ended + alive:
        confirmed = p["state"] == 2
        ref = int(p["conf"]) if confirmed else n - 1
        line = p["line"]
        level = PT._at(line, ref)
        d = int(p["dir"])
        name = _pattern_name(p)
        row = {"name": name, "direction": _DIR[d], "state": "confirmed" if confirmed else "forming", "kind": "chart",
               "points": [{"t": iso(idx[i]), "price": pr} for i, pr in p["pts"]], "trigger_level": level,
               "trigger_line": [{"t": iso(idx[line[1]]), "price": line[0]}, {"t": iso(idx[ref]), "price": level}],
               "alive": p["end"] is None, "known_at": iso(idx[p["start"]])}
        if any(i < w0 for i, _ in p["pts"]) or line[1] < w0:
            row["started_before"] = True
        if p.get("invalid") is not None:
            row["invalid_level"] = float(p["invalid"][0])
        if confirmed:
            row["confirmed_at"] = iso(idx[ref])
            row["text"] = (f"{name}: confirmed by the {iv} body close {'above' if d > 0 else 'below'} {_px(level)} "
                           f"({n - 1 - ref} candle(s) ago). The shape is complete; that is all it says.")
        elif d == 0:
            low = PT._at(p["line2"], ref)
            row["trigger_level_low"] = low
            row["text"] = (f"{name} forming: wait for a {iv} body close above {_px(level)} (up) or below {_px(low)} "
                           f"(down); inside the two lines it has no direction.")
        else:
            inv = p.get("invalid")
            row["text"] = (f"{name} forming: wait for a {iv} body close {'above' if d > 0 else 'below'} {_px(level)} "
                           f"to confirm it"
                           + (f"; a close {'above' if inv[1] > 0 else 'below'} {_px(inv[0])} cancels it" if inv else "")
                           + ". Until then it is only a shape.")
        out.append(row)
    return out


def candle_marks(ctx: Context, w0: int, zones: list[dict], b0: int) -> list[dict]:
    """Candlestick patterns on the last CANDLE_PATTERN_BARS closed candles (one point each)."""
    df = ctx.df
    n = len(df)
    idx = df.index
    k0 = max(w0, n - CANDLE_PATTERN_BARS)
    lo = max(0, k0 - 12)  # the tweezer test looks 10 candles back
    o, h, l, c = (df[x].to_numpy(float)[lo:n] for x in ("o", "h", "l", "c"))
    m = n - lo
    sup, res = np.full(m, np.nan), np.full(m, np.nan)
    spans = []
    for z in zones:  # candles (relative to b0) during which the zone was a line on the chart
        dead = z["expires_pos"]
        thru = [r["resolved_idx"] for r in z.get("reactions", []) if r["type"] in ("break", "retest_fail")]
        if len(thru) >= MAX_CLOSES_THROUGH:
            dead = min(dead, thru[MAX_CLOSES_THROUGH - 1] + 1)
        spans.append((float(np.ceil(z["members"][0]["known_pos"])), float(dead), z["price"]))
    for j in range(m):
        t = lo + j
        a = ctx.atr[t]
        if not np.isfinite(a) or a <= 0:
            continue
        live = [p for born, dead, p in spans if born <= t - b0 < dead]
        below = [p for p in live if p <= c[j]]
        above = [p for p in live if p > c[j]]
        if below:
            sup[j] = (c[j] - max(below)) / a
        if above:
            res[j] = (min(above) - c[j]) / a
    cs = PT.candle_patterns(o, h, l, c, ctx.atr_prev[lo:n], sup, res)
    out = []
    for t in range(k0, n):
        j = t - lo
        for col, (key, _) in enumerate(PT.CS_FEATURES):
            v = cs[j, col]
            if v == 0:
                continue
            signed = key != "cs_inside"
            up = v > 0
            name = _CS_NAMES[key][0 if up else 1]
            out.append({"name": name, "direction": ("up" if up else "down") if signed else None, "state": "confirmed",
                        "kind": "candlestick", "points": [{"t": iso(idx[t]), "price": float(l[j] if up else h[j])}],
                        "trigger_level": None, "text": f"{name} on the {ctx.interval} candle (closed {_px(c[j])})"})
    return out


def chart_sessions(times: pd.DatetimeIndex, t_end: pd.Timestamp, now: pd.Timestamp, interval: str) -> list[dict]:
    """Session windows overlapping the visible range (intervals up to 1h; the last SESSION_MAX_DAYS days).
    `start` / `end` are the first and last candle inside the window; exact times are in open_utc / close_utc."""
    if INTERVALS[interval][4] > 1.0 or len(times) == 0:
        return []
    times = ns_index(times)
    t0 = max(times[0], t_end - pd.Timedelta(days=SESSION_MAX_DAYS))
    days = int((now - t0).total_seconds() // 86400) + 3
    out = []
    for name, tzname, ot, ct in SESSIONS:
        for o_, c_ in _occurrences(ZoneInfo(tzname), ot, ct, now.to_pydatetime(), days=range(-days, 3)):
            a, b = pd.Timestamp(o_), pd.Timestamp(c_)
            if b <= t0 or a >= t_end:
                continue
            i = int(times.searchsorted(a, side="left"))
            j = int(times.searchsorted(b, side="left")) - 1
            if i > j and b <= t_end:
                continue  # no candle inside it (holiday / gap)
            row = {"name": name, "start": iso(times[i]) if i < len(times) else iso(a),
                   "end": iso(times[j]) if b <= t_end else iso(b), "open_utc": iso(a), "close_utc": iso(b)}
            if a < times[0]:
                row["started_before"] = True
            if b > t_end:
                row["in_progress"] = True
            out.append(row)
    return sorted(out, key=lambda r: r["open_utc"])


def _ff_type(title: str) -> str:
    t = (title or "").lower()
    if "non-farm" in t or "nonfarm" in t:
        return "nfp"
    if "cpi" in t:
        return "cpi"
    if "fomc" in t or "federal funds" in t:
        return "fomc"
    if "unemployment claims" in t or "jobless claims" in t:
        return "claims"
    if any(k in t for k in ("rate decision", "official bank rate", "main refinancing", "policy rate", "cash rate",
                            "monetary policy")):
        return "central_bank"
    return "other"


def chart_news(currencies: tuple, t_first: pd.Timestamp, now: pd.Timestamp, with_feed: bool) -> tuple[list[dict], list[str]]:
    """High-impact events from the window start to 24 h ahead: the official schedule table, plus (live only) the
    high-impact rows of this week's calendar feed that the table does not have. Rows carry the exact `time`."""
    notes = []
    rows = []
    hi = now + pd.Timedelta(hours=NEWS_AHEAD_H)
    for e in EV.history(",".join(currencies), iso(t_first), iso(hi)):
        t = pd.Timestamp(e["time_utc"])
        if t > now and not e.get("scheduled", True):
            continue  # an unscheduled event is not known before it happens
        rows.append({"time": t, "currency": e["currency"], "type": e["type"], "title": e["title"],
                     "source": "official schedule", "approximate": bool(e.get("approximate"))})
    if with_feed:
        try:
            past_h = min(168.0, max((now - t_first).total_seconds() / 3600, 0.0))
            cal = get_calendar(list(currencies), ["High"], days=NEWS_AHEAD_H / 24, past_hours=past_h,
                               now=now.to_pydatetime())
            for e in cal["events"]:
                t = pd.Timestamp(e["time_utc"])
                if any(r["currency"] == e["currency"] and abs(r["time"] - t) <= pd.Timedelta(minutes=10) for r in rows):
                    continue
                rows.append({"time": t, "currency": e["currency"], "type": _ff_type(e.get("title") or ""),
                             "title": e.get("title"), "source": "weekly calendar feed", "approximate": False})
        except Exception as e:
            notes.append(f"weekly calendar feed unavailable ({type(e).__name__}); news shows the official schedule only")
    else:
        notes.append("replay: news shows the official schedule table only (the weekly feed holds the current week)")
    return sorted(rows, key=lambda r: r["time"]), notes


def place_news(rows: list[dict], times: pd.DatetimeIndex, now: pd.Timestamp) -> list[dict]:
    """`t` = the candle that contains the event (events after the last candle keep their exact time)."""
    times = ns_index(times)
    out = []
    for r in rows:
        j = int(times.searchsorted(r["time"], side="right")) - 1
        nxt = times[j + 1] if 0 <= j < len(times) - 1 else None
        inside = j >= 0 and (nxt is not None or r["time"] <= now)
        out.append({"t": iso(times[j]) if inside else iso(r["time"]), "currency": r["currency"], "type": r["type"],
                    "title": r["title"], "past": bool(r["time"] <= now), "time": iso(r["time"]),
                    "minutes": round((r["time"] - now).total_seconds() / 60), "source": r["source"],
                    **({"approximate": True} if r["approximate"] else {})})
    return out


# ------------------------------------------------------------------ playbook, higher timeframes, reading

def public_playbook(pb: dict) -> dict:
    """The /mtf playbook with every level as a plain number."""
    def nums(tg) -> list[float]:
        return [float(t["price"]) for t in tg or []]

    def zone(z: Optional[dict]) -> Optional[dict]:
        if not z:
            return None
        return {"side": z["side"], "low": z["low"], "high": z["high"], "edge": z["edge"], "stop": z["stop"],
                "targets": nums(z["targets"]), "trigger": z["trigger"], "invalidation": z["invalidation"],
                "stop_note": z["stop_note"], "stop_inside_prior_sweeps": z["stop_inside_prior_sweeps"]}
    mode = pb["mode"]
    box = pb.get("box")
    z = cancel = None
    if mode == "break_retest":
        z, cancel = [pb["retest_zone"]["low"], pb["retest_zone"]["high"]], pb["cancel"]
    elif mode == "trend_pullback":
        z, cancel = [pb["pullback_zone"]["low"], pb["pullback_zone"]["high"]], pb["invalidation"]
    elif mode == "range":
        cancel = (f"a {pb['timeframe']} body close above {_px(box['top'])} or below {_px(box['bottom'])} ends the "
                  f"range plan (then: break → retest of that edge)")
    else:
        cancel = pb.get("why")
    cases = [{"action": c["action"], "when": c["when"], "entry_price": c["entry_price"], "stop": c["stop"],
              "targets": nums(c["targets"]), "target_names": [t["name"] for t in c["targets"]], "reason": c["reason"],
              "risk": c["risk"], "entry": c["entry"], "trigger_tf": c["trigger_tf"]} for c in pb.get("cases", [])]
    return {"mode": mode, "direction": pb.get("direction"), "timeframe": pb.get("timeframe"), "zone": z,
            "sell_zone": zone(pb.get("sell_zone")), "buy_zone": zone(pb.get("buy_zone")), "cancel": cancel,
            "stop": pb.get("stop"), "targets": nums(pb.get("targets")), "trigger": pb.get("trigger"),
            "box": {k: box[k] for k in ("top", "bottom", "mid")} if box else None,
            "price_position": pb.get("price_position"), "price_now": pb.get("price_now"),
            "price_back_through_edge": pb.get("price_back_through_edge"),
            "distance_to_retest_pips": pb.get("distance_to_retest_pips"), "cases": cases,
            "reasons": pb.get("reasons", [])}


def _direction(tf: dict) -> int:
    """+1 / -1 from the regime (impulse or trend), else from the body-swing trend, else 0."""
    r = tf["regime"]
    if r.endswith("_up"):
        return 1
    if r.endswith("_down"):
        return -1
    return {"up": 1, "down": -1}.get(tf["swing_trend"], 0)


def pullback_of(ctx: Context) -> Optional[dict]:
    """Same definition as the dataset's pullback_dir / pullback_depth, for the last closed candle: trend intact
    by body swings and price between the last swing high and the last swing low."""
    sw = ctx.run.swings
    h1 = next((s for s in reversed(sw) if s.kind == "H"), None)
    l1 = next((s for s in reversed(sw) if s.kind == "L"), None)
    trend = ctx.run.trend[-1]
    if h1 is None or l1 is None or trend not in ("up", "down"):
        return None
    close = float(ctx.df["c"].iloc[-1])
    span = h1.price - l1.price
    if span <= 0 or not l1.price < close < h1.price:
        return None
    up = trend == "up"
    depth = (h1.price - close) / span if up else (close - l1.price) / span
    return {"dir": trend, "depth": float(min(max(depth, 0.0), 1.5)),
            "meaning": (f"{trend}trend intact by body swings; price is {depth * 100:.0f}% of the way from the last swing "
                        f"{'high' if up else 'low'} {_px(h1.price if up else l1.price)} back to the protected "
                        f"{'higher low' if up else 'lower high'} {_px(l1.price if up else h1.price)}")}


def where_in(box: dict, px: float) -> tuple[float, str]:
    """(percent of the box height, near_top / near_bottom / mid_box / above / below) for a price."""
    height = box["top"] - box["bottom"]
    pct = (px - box["bottom"]) / height * 100 if height > 0 else 50.0
    return pct, ("above" if pct > 100 else "below" if pct < 0 else "near_top" if pct >= 75 else
                 "near_bottom" if pct <= 25 else "mid_box")


_WHERE = {"near_top": "near the top", "near_bottom": "near the bottom", "mid_box": "mid-range",
          "above": "above the top on this interval's closes (no close of its own above yet)",
          "below": "below the bottom on this interval's closes (no close of its own below yet)"}


def plain_phrase(tf: dict, now_px: float) -> str:
    """One plain clause about a timeframe; a box position uses the freshest closed price."""
    iv, box, rg = tf["interval"], tf.get("box"), tf["regime"]
    imp = tf.get("last_impulse")
    if box:
        rng = f"{_px(box['bottom'])}–{_px(box['top'])}"
        p = box["position"]
        if p["state"] == "inside":
            pct, w = where_in(box, now_px)
            return f"{iv} is ranging {rng} with price {_WHERE[w]}" + (f" ({pct:.0f}%)" if 0 <= pct <= 100 else "")
        side = "above" if p["state"] == "closed_above" else "below"
        return (f"{iv} closed {side} its {rng} range {p['candles_ago']} candle(s) ago (close {_px(p['break_close'])})"
                + (", not yet retested" if p["candles_ago"] <= 3 else ""))
    if rg.startswith("impulse") and imp:
        return (f"{iv} is in an impulsive {'rally' if imp['direction'] == 'up' else 'drop'} {_px(imp['from'])} → "
                f"{_px(imp['to'])} ({imp['pips']:.0f} pips in {imp['candles']} candles)")
    if rg.startswith("trend"):
        sw = " ".join(f"{x['label']} {_px(x['price'])}" for x in tf["swings"][-2:])
        return f"{iv} is trending {rg.split('_')[1]} by body swings ({sw})"
    return f"{iv} has no clean structure (mixed swings, no box)"


def plan_sentence(pb: dict, iv: str) -> str:
    m = pb["mode"]
    if m == "range":
        s, b = pb["sell_zone"], pb["buy_zone"]
        return (f"Plan: only the edges of the {pb['timeframe']} range matter (sell zone {_px(s['low'])}–{_px(s['high'])}, "
                f"buy zone {_px(b['low'])}–{_px(b['high'])}); price is {_WHERE[pb['price_position']]} now.")
    if m == "break_retest":
        z = pb["retest_zone"]
        at = pb.get("price_back_through_edge") or z["low"] <= pb["price_now"] <= z["high"]
        return (f"Plan: {pb['direction']} only on a rejected retest of the broken {pb['timeframe']} edge "
                f"{_px(pb['broken_edge'])}; "
                + (f"price is at that edge now, so the next {iv} closes decide it." if at else
                   f"price is {pb['distance_to_retest_pips']:.0f} pips away from it."))
    if m == "trend_pullback":
        z = pb["pullback_zone"]
        return (f"Plan: {pb['direction']} from a pullback into {_px(z['low'])}–{_px(z['high'])} on {pb['timeframe']}; "
                f"void on {pb['invalidation']}.")
    return f"No plan: {pb.get('why', 'no setup')}."


def higher_block(tfs: dict[str, dict], ctxs: dict[str, Context], order: list[str], interval: str,
                 now_px: float) -> dict:
    out = {}
    for iv in order:
        if iv == interval:
            continue
        tf = tfs[iv]
        box = tf.get("box")
        b = None
        if box:
            p = box["position"]
            pct, w = where_in(box, now_px)
            b = {"top": box["top"], "bottom": box["bottom"], "state": _BOX_STATE[p["state"]],
                 "position_pct": pct, "where": w,
                 "fakeouts": box.get("fakeouts_above", 0) + box.get("fakeouts_below", 0)}
        out[iv] = {"regime": tf["regime"], "swing_trend": tf["swing_trend"], "box": b,
                   "pullback": pullback_of(ctxs[iv]), "direction": _DIR[_direction(tf)],
                   "last_closed": tf["last_closed_candle_close"]}
    return out


def phase_of(tf: dict, pb: dict, ctx: Context) -> str:
    """One of PHASES for this interval, read inside the higher-timeframe plan."""
    box = tf.get("box")
    pos = box["position"] if box else None
    edge = ("near_top", "near_bottom")
    if pb["mode"] == "break_retest":  # the higher-timeframe plan comes first: it is what the entry waits on
        return "break_awaiting_retest"
    if pb["mode"] == "range" and pb.get("price_position") in edge:
        return "range_edge"
    if pos and pos["state"] != "inside":
        return "break_awaiting_retest"
    if pos and pos.get("where") in edge:
        return "range_edge"
    if tf["regime"].startswith("impulse"):
        return "impulse"
    if box or not tf["regime"].startswith("trend"):
        return "consolidation"
    up = tf["regime"].endswith("_up")
    sw = ctx.run.swings
    last_kind = sw[-1].kind if sw else None
    h1 = next((s for s in reversed(sw) if s.kind == "H"), None)
    l1 = next((s for s in reversed(sw) if s.kind == "L"), None)
    close = tf["last_close"]
    if h1 is not None and l1 is not None:
        if (up and close > h1.price) or (not up and close < l1.price):
            return "continuation"  # already closed beyond the last swing in the trend direction
        if (up and close < l1.price) or (not up and close > h1.price):
            return "corrective_retracement"  # closed through the protected swing: the trend label is in doubt
    return "corrective_retracement" if last_kind == ("H" if up else "L") else "continuation"


def entry_direction(tf: dict, pb: dict) -> tuple[int, str]:
    """Direction of the entry the plan points to right now (0 = none), and where it comes from."""
    m = pb["mode"]
    if m in ("break_retest", "trend_pullback"):
        return (1 if pb["direction"] == "long" else -1), f"the {m.replace('_', ' ')} plan ({pb['direction']})"
    if m == "range":
        w = pb.get("price_position")
        if w == "near_top":
            return -1, f"a sell at the {pb['timeframe']} range top"
        if w == "near_bottom":
            return 1, f"a buy at the {pb['timeframe']} range bottom"
        return 0, "mid-range: no entry"
    d = _direction(tf)
    return d, (f"an entry with this interval's {tf['regime'].replace('_', ' ')}" if d else
               "stand aside: no box and no aligned trend")


def reference_box(tf: dict, tfs: dict[str, dict], pb: dict) -> Optional[tuple[str, dict, Optional[str]]]:
    """(interval, box, where price is in it now) of the box risk is judged on: this interval's own active box,
    else the plan's range box (position from this interval's last close)."""
    box = tf.get("box")
    if box and box["position"]["state"] == "inside":
        return tf["interval"], box, box["position"].get("where")
    if pb["mode"] == "range" and pb.get("timeframe") in tfs and tfs[pb["timeframe"]].get("box"):
        return pb["timeframe"], tfs[pb["timeframe"]]["box"], pb.get("price_position")
    return (tf["interval"], box, None) if box else None


def assess_risk(f: dict) -> dict:
    """The stated rule (RISK_RULE) applied to plain inputs. Keys of `f`:
    stop (price distance of the fixed stop), pip, atr, interval, news_minutes (signed minutes to the nearest
    high-impact event, None = none known), news_title, box_where ('mid_box' / 'near_top' / 'near_bottom' / None),
    box_name, entry_dir, entry_what, htf_dir, htf_name, fakeouts, sessions (active session names), asia_open,
    market_open, data_age_candles."""
    reasons = []
    factors = {}
    iv = f.get("interval", "")
    atr = f.get("atr")
    factors["stop_inside_noise"] = bool(atr and np.isfinite(atr) and f["stop"] < 0.5 * atr)
    if factors["stop_inside_noise"]:
        pip = f.get("pip") or 0
        reasons.append((f"the fixed {f['stop'] / pip:.0f}-pip stop is {f['stop'] / atr:.2f} of one {iv} candle's ATR "
                        f"({atr / pip:.0f} pips)" if pip else
                        f"the fixed stop ({f['stop']:.5g}) is {f['stop'] / atr:.2f} of one {iv} candle's ATR "
                        f"({atr:.5g})") + ": ordinary candle noise reaches it")
    nm = f.get("news_minutes")
    factors["news_within_60m"] = nm is not None and abs(nm) <= NEWS_RISK_MIN
    if factors["news_within_60m"]:
        reasons.append(f"high-impact news {'in ' + str(int(nm)) + ' min' if nm >= 0 else str(int(-nm)) + ' min ago'}: "
                       f"{f.get('news_title') or 'scheduled event'}")
    factors["mid_box"] = f.get("box_where") == "mid_box"
    if factors["mid_box"]:
        reasons.append(f"price is mid-box on {f.get('box_name', 'the box')}: no edge and no level to put a stop behind")
    ed, hd = f.get("entry_dir") or 0, f.get("htf_dir") or 0
    factors["against_higher_timeframe"] = bool(ed and hd and ed != hd)
    if factors["against_higher_timeframe"]:
        reasons.append(f"{f.get('entry_what', 'the entry')} goes against {f.get('htf_name', 'the higher timeframe')} "
                       f"({'up' if hd > 0 else 'down'})")
    factors["fakeouts_on_box"] = (f.get("fakeouts") or 0) >= 2
    if factors["fakeouts_on_box"]:
        reasons.append(f"{f['fakeouts']} fakeouts on {f.get('box_name', 'the current box')}: its edges have already "
                       f"given false closes")
    ses = f.get("sessions") or []
    thin = not any(s in ("London", "New York") for s in ses) and not f.get("asia_open")
    factors["thin_session"] = bool(thin)
    if thin:
        reasons.append(f"thin session ({', '.join(ses) or 'off-session'}): neither London nor New York is open and it "
                       f"is not the Tokyo open")
    age = f.get("data_age_candles")
    closed = f.get("market_open") is False
    factors["closed_or_stale"] = bool(closed or (age is not None and age > 2))
    if factors["closed_or_stale"]:
        reasons.append("the market is closed" if closed else
                       f"the last closed {iv} candle is {age:.1f} candles old: the picture may have changed")
    k = sum(factors.values())
    ed_txt = f.get("entry_what")
    note = ("Risk rates the conditions for an entry; it is not a signal."
            + (f" Right now the plan has no entry ({ed_txt})." if not ed and ed_txt else ""))
    return {"level": "high" if k >= 3 else "medium" if k >= 1 else "low", "reasons": reasons, "rule": RISK_RULE,
            "factors": factors, "count": int(k), "note": note}


def _wait_for(tf: dict, pb: dict, st: BoxState, n: int, patterns: list[dict], iv: str) -> list[str]:
    out = []
    m = pb["mode"]
    if m == "range":
        s, b = pb["sell_zone"], pb["buy_zone"]
        where = pb.get("price_position")
        first, second = (s, b) if where != "near_bottom" else (b, s)
        if where == "mid_box":
            out.append(f"price to reach an edge of the {pb['timeframe']} range: {_px(s['low'])}–{_px(s['high'])} (top) "
                       f"or {_px(b['low'])}–{_px(b['high'])} (bottom); mid-box there is nothing to do")
        out += [first["trigger"], second["trigger"]]
    elif m == "break_retest":
        E, down = pb["broken_edge"], pb["direction"] == "short"
        z = pb["retest_zone"]
        last = pb.get("price_now", tf["last_close"])
        side = "below" if down else "above"
        if pb.get("price_back_through_edge") or z["low"] <= last <= z["high"]:
            out.append(f"price is at the retest now ({_px(last)}, zone {_px(z['low'])}–{_px(z['high'])}): wait for a "
                       f"{iv} candle whose BODY closes back {side} {_px(E)} (a wick through it is normal); without "
                       f"that close there is no entry")
        else:
            out.append(pb["trigger"])
        out.append("if instead: " + pb["cancel"])
    elif m == "trend_pullback":
        out.append(pb["trigger"])
        out.append("if instead: " + pb["invalidation"])
    else:
        out.append(f"a box or an aligned trend on the higher timeframes ({pb.get('why', 'no setup')})")
    if st.status[n - 1] in (OUT_UP, OUT_DOWN):
        k = n - 1 - int(st.break_idx[n - 1])
        if k < FAKEOUT_MAX:
            up = st.status[n - 1] == OUT_UP
            edge = float(st.top[n - 1] if up else st.bot[n - 1])
            out.append(f"{FAKEOUT_MAX - k} more {iv} close(s) {'above' if up else 'below'} {_px(edge)}: the close outside "
                       f"this interval's box is {k} candle(s) old, and a close back inside makes it a fakeout")
    for p in patterns:
        if p["kind"] == "chart" and p["state"] == "forming" and p["alive"]:
            out.append(p["text"])
    return out[:6]


def build_reading(tfs: dict[str, dict], ctxs: dict[str, Context], order: list[str], interval: str, pb: dict,
                  st: BoxState, patterns: list[dict], pip: float, sl_pips: float) -> tuple[dict, dict]:
    """(reading without the time-dependent parts, risk inputs that do not depend on the clock)."""
    tf = tfs[interval]
    ctx = ctxs[interval]
    n = len(ctx.df)
    highers = [iv for iv in order if iv != interval]
    now_px = tf["last_close"]
    lead = tf.get("lead_in_impulse") if tf.get("box") else None
    s1 = plain_phrase(tf, now_px) + (f", after an impulsive {'rally' if lead['direction'] == 'up' else 'drop'} "
                                     f"{_px(lead['from'])} → {_px(lead['to'])}" if lead else "") + "."
    s2 = ("Higher up, " + "; ".join(plain_phrase(tfs[iv], now_px) for iv in reversed(highers)) + "."
          if highers else "No higher timeframe data.")
    s3 = plan_sentence(pb, interval)
    cont = tf["continuation"]
    what_next = [f"Up ({interval}): {cont['up']}", f"Down ({interval}): {cont['down']}"]
    biv = pb.get("timeframe")
    if biv and biv != interval and biv in tfs and tfs[biv].get("box"):
        c2 = tfs[biv]["continuation"]
        what_next += [f"Up ({biv} box): {c2['up']}", f"Down ({biv} box): {c2['down']}"]
    ref = reference_box(tf, tfs, pb)
    ed, what = entry_direction(tf, pb)
    hd, hname = 0, "the higher timeframe"
    for iv in highers[::-1]:  # nearest higher timeframe first
        d = _direction(tfs[iv])
        if d:
            rg = tfs[iv]["regime"]
            hd, hname = d, (f"the {iv} {rg.replace('_', ' ')}" if rg.endswith(("_up", "_down")) else
                            f"the {iv} swing trend")
            break
    static_risk = {
        "stop": sl_pips * pip, "pip": pip, "atr": tf["atr"], "interval": interval,
        "box_where": ref[2] if ref else None,
        "box_name": f"the {ref[0]} box {_px(ref[1]['bottom'])}–{_px(ref[1]['top'])}" if ref else None,
        "entry_dir": ed, "entry_what": what, "htf_dir": hd, "htf_name": hname,
        "fakeouts": (ref[1].get("fakeouts_above", 0) + ref[1].get("fakeouts_below", 0)) if ref else 0,
    }
    reading = {"regime": tf["regime"], "phase": phase_of(tf, pb, ctx), "summary": " ".join(x for x in (s1, s2, s3) if x),
               "what_next": what_next, "wait_for": _wait_for(tf, pb, st, n, patterns, interval)}
    return reading, static_risk


# ------------------------------------------------------------------ assembly

def build_chart(ctxs: dict[str, Context], interval: str, bars: int, pip: float, sl_pips: float = 20,
                tp_pips: float = 50, tp2_pips: float = 100, now: Optional[pd.Timestamp] = None,
                odds: Optional[dict] = None, news: Optional[list[dict]] = None) -> dict:
    """Pure (no I/O): contexts of CLOSED candles -> everything that does not change until the next close.
    Keys starting with `_` are inputs of `finish()`."""
    ctx = ctxs[interval]
    n = len(ctx.df)
    now = now or ctx.closes[-1]
    order = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
    w0 = max(0, n - int(bars))
    sts = {iv: box_states(c) for iv, c in ctxs.items()}
    st = sts[interval]
    res = analyze_mtf(ctxs, pip, sl_pips, tp_pips, tp2_pips, states=sts)
    tfs, pb = res["timeframes"], res["playbook"]
    if odds:
        pb["cases"] = build_cases(tfs, res["stack"], pb, pip, odds.get(pb.get("timeframe")) or next(iter(odds.values())))
    df = ctx.df
    lo, hi = float(df["l"].iloc[w0:].min()), float(df["h"].iloc[w0:].max())
    last = float(df["c"].iloc[-1])
    zones, alive, b0 = chart_zones(ctxs, order, interval, w0, pip, now)
    zrows = visible_zones(alive, lo, hi, last, pip, now, order)
    boxes = chart_boxes(ctx, st, w0)
    for iv in order:
        if iv != interval:
            hb = higher_box(tfs[iv], ctx, w0)
            if hb:
                boxes.append(hb)
    log: list = []
    pctx, off = tail_context(ctx, int(bars) + PATTERN_WARMUP)
    PT.chart_pattern_state(pctx, log)
    patterns = chart_patterns(pctx, w0 - off, log) + candle_marks(ctx, w0, zones, b0)
    reading, static_risk = build_reading(tfs, ctxs, order, interval, pb, st, patterns, pip, sl_pips)
    dur = pd.Timedelta(hours=INTERVALS[interval][4])
    t_end = ctx.closes[-1] + dur  # the visible range includes the forming candle's slot
    return {
        "interval": interval, "pip": pip, "atr": float(ctx.atr[-1]),
        "last_closed": iso(ctx.closes[-1]), "last_close": last,
        "params": {"bars": int(bars), "sl_pips": sl_pips, "tp_pips": tp_pips, "tp2_pips": tp2_pips,
                   "timeframes": order},
        "candles": chart_candles(ctx, w0),
        "swings": chart_swings(ctx, w0),
        "boxes": boxes,
        "zones": zrows,
        "events": chart_events(ctx, st, w0, pip, zones, b0, lo, hi, {z["id"] for z in zrows}),
        "impulses": chart_impulses(ctx, w0, pip),
        "patterns": patterns,
        "sessions": chart_sessions(df.index[w0:], t_end, now, interval),
        "playbook": public_playbook(pb),
        "higher": higher_block(tfs, ctxs, order, interval, last),
        "reading": reading,
        "_news": news or [], "_risk": static_risk, "_times": df.index[w0:], "_closes_last": ctx.closes[-1],
        "_dur": dur, "_range": (lo, hi),
    }


def trading_gap(a: pd.Timestamp, b: pd.Timestamp) -> pd.Timedelta:
    """b - a without the FX weekend (Friday 17:00 -> Sunday 17:00 New York), so a Monday is not 'stale'."""
    if b <= a:
        return pd.Timedelta(0)
    total = b - a
    ny = a.tz_convert("America/New_York")
    d = ny.date() - pd.Timedelta(days=(ny.dayofweek - 4) % 7 + 7).to_pytimedelta()
    while True:
        fri = pd.Timestamp(d.year, d.month, d.day, 17, tz="America/New_York")
        if fri >= b:
            break
        s2 = d + pd.Timedelta(days=2).to_pytimedelta()
        sun = pd.Timestamp(s2.year, s2.month, s2.day, 17, tz="America/New_York")
        lo, hi = max(a, fri), min(b, sun)
        if hi > lo:
            total -= hi - lo
        d = d + pd.Timedelta(days=7).to_pytimedelta()
    return total


def _asia_open(now: pd.Timestamp) -> bool:
    name, tzname, ot, ct = next(s for s in SESSIONS if s[0] == "Tokyo")
    for o_, _ in _occurrences(ZoneInfo(tzname), ot, ct, now.to_pydatetime(), days=range(-1, 2)):
        if pd.Timestamp(o_) <= now < pd.Timestamp(o_) + pd.Timedelta(hours=ASIA_OPEN_HOURS):
            return True
    return False


def finish(static: dict, now: pd.Timestamp, forming: Optional[dict] = None, price: Optional[dict] = None,
           fx_like: bool = True) -> dict:
    """Add what moves between closes: the forming candle, `price`, the news `past` flags and the risk."""
    out = {k: v for k, v in static.items() if not k.startswith("_")}
    candles = list(static["candles"])
    last = static["last_close"]
    px, px_src, px_time = last, "last closed candle", static["last_closed"]
    if forming:
        candles.append({"t": forming["start"], "o": forming["o"], "h": forming["h"], "l": forming["l"],
                        "c": forming["last"], "forming": True, "closes_at": forming["close_time"]})
        px, px_src, px_time = forming["last"], "forming candle", iso(now)
    atr = static["atr"]
    if price and price.get("price") is not None and np.isfinite(atr) and abs(float(price["price"]) - px) <= atr:
        # a fresher quote than the candle feed, used only when it agrees with the candles to within one ATR
        px, px_src, px_time = float(price["price"]), str(price.get("source") or "live quote"), price.get("as_of")
        if forming:
            f = candles[-1]
            f.update(c=px, h=max(f["h"], px), l=min(f["l"], px))
        elif pd.Timedelta(0) <= now - static["_closes_last"] < static["_dur"]:
            # the candle feed has not delivered the current candle yet (delayed feed): show the quote as a stub
            # that opens at the last close; its high / low are not known, and it says so
            forming = {"start": iso(static["_closes_last"])}
            candles.append({"t": forming["start"], "o": last, "h": max(last, px), "l": min(last, px), "c": px,
                            "forming": True, "synthetic": True,
                            "closes_at": iso(static["_closes_last"] + static["_dur"])})
    out["candles"] = candles
    out["price"], out["price_source"], out["price_as_of"] = px, px_src, px_time
    out["as_of"] = iso(now)
    times = static["_times"]
    if forming:
        times = times.append(pd.DatetimeIndex([pd.Timestamp(forming["start"])]))
    news = place_news(static["_news"], times, now)
    out["news"] = news
    near = min((x for x in news), key=lambda x: abs(x["minutes"]), default=None)
    tag = session_tag(now)
    gap = trading_gap(static["_closes_last"], now) if fx_like else now - static["_closes_last"]
    age = gap / static["_dur"]
    risk = assess_risk({**static["_risk"], "news_minutes": near["minutes"] if near else None,
                        "news_title": f"{near['currency']} {near['title']}" if near else None,
                        "sessions": [s for s in tag["sessions"] if s != "off-session"], "asia_open": _asia_open(now),
                        "market_open": fx_market_open(now.to_pydatetime()) if fx_like else None,
                        "data_age_candles": float(age)})
    wait = list(static["reading"]["wait_for"])
    soon = [x for x in news if 0 <= x["minutes"] <= NEWS_RISK_MIN]
    if soon:
        x = soon[0]
        wait.insert(0, f"the {x['currency']} {x['title']} release in {x['minutes']} min: let the "
                       f"{static['interval']} candle that contains it close before reading anything")
    out["reading"] = {**static["reading"], "wait_for": wait, "risk": risk,
                      "session": "+".join(tag["sessions"]) + (f" ({tag['transition']})" if tag["transition"] else "")}
    return out


# ------------------------------------------------------------------ service

_odds_pending: set = set()
_odds_slot = threading.Semaphore(1)  # one background odds computation at a time (replay scrubbing must not pile up)


def _odds_for(sym: Symbol, ctxs: dict[str, Context], order: list[str], pip: float, tp_pips: float) -> tuple[dict, bool]:
    """Measured odds from the /mtf cache. Missing ones are computed in the background (they take a while and
    live for 6 hours); the chart does not wait for them."""
    rk = replay_as_of()
    rk = iso(rk)[:10] if rk is not None else None
    odds, pending = {}, False
    for iv in [i for i in order if INTERVALS[i][4] >= 1.0][:2] or order[:1]:
        ctx = ctxs[iv]
        key = ("mtf_odds", sym.id, iv, pip, tp_pips, FAKEOUT_MAX, rk, len(ctx.df) // 50)
        hit = cache.get(key)
        if hit is not None:
            odds[iv] = hit
            continue
        pending = True
        if key in _odds_pending or not _odds_slot.acquire(blocking=False):
            continue  # already running, or the worker is busy: the next rebuild asks again
        _odds_pending.add(key)

        def work(key=key, ctx=ctx):
            try:
                cache.get_or_set(key, TTL_MODEL, lambda: timeframe_odds(ctx, pip, tp_pips))
            except Exception:
                pass
            finally:
                _odds_pending.discard(key)
                _odds_slot.release()
        threading.Thread(target=contextvars.copy_context().run, args=(work,), daemon=True).start()
    return odds, pending


def _build_static(sym: Symbol, interval: str, bars: int, pip: float, sl_pips: float, tp_pips: float,
                  tp2_pips: float, now: pd.Timestamp, replaying: bool) -> dict:
    ctxs, needs = {}, []
    for iv in (interval, *HIGHER[interval]):
        try:
            ctxs[iv] = build_context(sym, iv, now, max_bars=MAX_CTX_BARS, with_htf=False)
        except Exception as e:
            if iv == interval:
                raise
            needs.append({"interval": iv, "reason": f"{type(e).__name__}: {str(e)[:160]}"})
    order = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
    odds, pending = _odds_for(sym, ctxs, order, pip, tp_pips)
    from .dataset import news_currencies
    w0 = max(0, len(ctxs[interval].df) - bars)
    news, notes = chart_news(news_currencies(sym), ctxs[interval].df.index[w0], now, with_feed=not replaying)
    static = build_chart(ctxs, interval, bars, pip, sl_pips, tp_pips, tp2_pips, now, odds, news)
    info = ctxs[interval].source_info or {"source": ctxs[interval].source}
    dn = make_data_note(sym, info)
    static.update(
        symbol=sym.id, source=info.get("source"),
        data_note=f"{dn['feed_short']}. {dn['h4_grid']}. Levels can differ from your broker's chart by a few "
                  f"pips or dollars: compare the shape and which side of a line a candle closed.",
        data_note_detail=dn, needs=needs, odds_pending=pending, patterns_note=PATTERN_NOTE,
        notes=notes + [
            "All times are candle OPEN times of this interval. Bodies and closes define structure; wicks are "
            "sweeps. A box is drawn from its first swing, but it was only known from `known_at`.",
            "`break_up` / `break_down` with of=swing is a body close through the protected swing of the swing "
            "trend (a close against the trend); with of=box a body close outside a consolidation box. Candlestick patterns are in `patterns` with kind="
            "candlestick (one point each, last 30 candles).",
            "An analysis tool, not financial advice."])
    return static


def chart(sym: Symbol, interval: str = "15m", bars: int = 300, pip: Optional[float] = None, sl_pips: float = 20,
          tp_pips: float = 50, tp2_pips: float = 100) -> dict:
    if interval not in HIGHER:
        raise ValueError(f"interval must be one of {', '.join(HIGHER)}")
    if not 50 <= int(bars) <= 1000:
        raise ValueError("bars must be between 50 and 1000")
    pip_used = pip or default_pip(sym)
    now = now_ts()
    replaying = replay_as_of() is not None
    series = get_series(sym, interval)
    df, closes, forming, _ = closed_frame(series.df, interval, day_close_mode(sym), now)
    if len(df) < 30:
        raise DataError(f"not enough closed {interval} candles for {sym.id} ({len(df)})")
    key = ("chart", sym.id, interval, int(bars), pip_used, sl_pips, tp_pips, tp2_pips, replaying, iso(closes[-1]),
           len(df))
    static = cache.get(key)
    if static is None:
        with cache._key_lock(key):  # one build per closed candle, however many polls arrive together
            static = cache.get(key)
            if static is None:
                static = _build_static(sym, interval, int(bars), pip_used, sl_pips, tp_pips, tp2_pips, now,
                                       replaying)
                cache.set(key, static, STATIC_TTL_PENDING if static["odds_pending"] else STATIC_TTL)
    price = None
    if not replaying:
        try:
            price = get_price(sym)
        except Exception:
            price = None
    out = finish(static, now, None if replaying else forming, price, fx_like=is_fx_like(sym))
    order_keys = ["symbol", "interval", "pip", "as_of", "price", "data_note"]
    return {**{k: out[k] for k in order_keys}, **{k: v for k, v in out.items() if k not in order_keys}}
