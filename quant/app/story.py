"""Session story: how did each session act -> where are we now -> what do we do next.

Sessions are SEQUENTIAL blocks of a trading day D (New York date, day rolls at 17:00 NY), DST-aware:
  Asia      17:00 New York (D-1)  -> London open 08:00 London (D)      (Sydney + Tokyo hours)
  London    08:00 London (D)      -> New York open 08:00 New York (D)
  New York  08:00 New York (D)    -> 17:00 New York (D)
Bodies decide structure (break = a candle CLOSE beyond), wicks only show liquidity taken (sweep).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .calendar import get_calendar
from .data import iso, now_ts, replay_as_of
from .retest import Level, default_pip, find_retests, levels_from_context
from .sessions import SESSIONS, fx_market_open
from .structure import Context, bodies, build_context
from .symbols import Symbol

NY, LDN = ZoneInfo("America/New_York"), ZoneInfo("Europe/London")
_OPEN = {name: (ZoneInfo(tz), ot) for name, tz, ot, _ in SESSIONS}
BLACKOUT_MIN = 15
FLAT_FRACTION = 0.2  # |net| below this share of the range = "flat"
TREND_FRACTION = 0.6


def session_blocks(d: date) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """(name, start_utc, end_utc) for trading day `d` (a weekday)."""
    ny_close_prev = datetime.combine(d - timedelta(days=1), time(17, 0), NY)
    ldn_tz, ldn_open_t = _OPEN["London"]
    ny_tz, ny_open_t = _OPEN["New York"]
    ldn_open = datetime.combine(d, ldn_open_t, ldn_tz)
    ny_open = datetime.combine(d, ny_open_t, ny_tz)
    ny_close = datetime.combine(d, time(17, 0), NY)
    u = lambda x: pd.Timestamp(x.astimezone(timezone.utc))  # noqa: E731
    return [("Asia", u(ny_close_prev), u(ldn_open)), ("London", u(ldn_open), u(ny_open)),
            ("New York", u(ny_open), u(ny_close))]


def trade_date(ts: pd.Timestamp) -> date:
    """New York trading date of a timestamp (rolls at 17:00 NY)."""
    return (ts.tz_convert(NY) + pd.Timedelta(hours=7)).date()


def recent_trading_days(now: pd.Timestamp, n: int) -> list[date]:
    d = trade_date(now)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out[::-1]


def extreme_status(closes: np.ndarray, wicks: np.ndarray, level: float, side: str) -> str:
    """'break' if any candle CLOSED beyond `level`, 'sweep' if only a wick went beyond, else 'untouched'."""
    if side == "high":
        if (closes > level).any():
            return "break"
        return "sweep" if (wicks > level).any() else "untouched"
    if (closes < level).any():
        return "break"
    return "sweep" if (wicks < level).any() else "untouched"


def session_stats(df: pd.DataFrame, pip: float) -> Optional[dict]:
    if df.empty:
        return None
    bh, bl = bodies(df)
    o, c = float(df["o"].iloc[0]), float(df["c"].iloc[-1])
    wh, wl = float(df["h"].max()), float(df["l"].min())
    rng = (wh - wl) / pip
    net = (c - o) / pip
    direction = "flat" if rng <= 0 or abs(net) < FLAT_FRACTION * rng else ("up" if net > 0 else "down")
    return {"open": o, "close": c, "body_high": float(bh.max()), "body_low": float(bl.min()), "wick_high": wh,
            "wick_low": wl, "range_pips": rng, "body_range_pips": float(bh.max() - bl.min()) / pip,
            "net_pips": net, "direction": direction, "high_time": iso(df["h"].idxmax()),
            "low_time": iso(df["l"].idxmin()), "candles": int(len(df))}


def character(stats: dict, prev_high: Optional[str], prev_low: Optional[str], held_retests: int) -> str:
    rng, net, d = stats["range_pips"], stats["net_pips"], stats["direction"]
    if prev_high == "sweep" and prev_low != "break" and d == "down":
        return "sweep high then reverse"
    if prev_low == "sweep" and prev_high != "break" and d == "up":
        return "sweep low then reverse"
    if prev_high == "sweep" and prev_low == "sweep":
        return "swept both sides (range)"
    brk = "up" if prev_high == "break" and d == "up" else "down" if prev_low == "break" and d == "down" else None
    if brk and held_retests:
        return f"breakout {brk} + retest hold"
    if rng > 0 and abs(net) >= TREND_FRACTION * rng and d in ("up", "down"):
        return f"trend {d}"
    if brk:
        return f"breakout {brk}"
    if prev_high == "break" and prev_low == "break":
        return "expansion both ways"
    return "range"


def _line_events(events: list[dict], ctx: Context, start, end) -> list[dict]:
    """What happened to body-level lines inside [start, end)."""
    closes, idx = ctx.closes, ctx.df.index
    out = []
    for e in events:
        lv: Level = e["lv"]
        b, m = e["break_idx"], e["touch_idx"]
        if start <= idx[b] < end:
            out.append({"event": "broken_up" if e["d"] > 0 else "broken_down", "time": iso(closes[b]),
                        "level": lv.price, "level_type": lv.label, "level_interval": lv.interval})
        if m is not None and start <= idx[m] < end:
            out.append({"event": ("retest_held" if e["rejected"] else "retest_failed_closed_back_through"),
                        "time": iso(closes[m]), "level": lv.price, "level_type": lv.label,
                        "level_interval": lv.interval, "wick_depth_pips": e.get("wick_depth_pips"),
                        "direction": "long" if e["d"] > 0 else "short"})
    out.sort(key=lambda x: x["time"])
    return out


def build_story(ctx: Context, lvl_ctx: Optional[Context], pip: float, days: int = 3, now: Optional[pd.Timestamp] = None,
                sl_pips: float = 25, tp_pips: float = 75, tol_pips: float = 3, max_wait: int = 48,
                fx_like: bool = True) -> dict:
    """Pure (no I/O): everything is derived from the closed candles in ctx / lvl_ctx."""
    now = now or now_ts()
    df = ctx.df
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    levels = (levels_from_context(lvl_ctx) if lvl_ctx is not None and lvl_ctx.interval != ctx.interval else []) \
        + levels_from_context(ctx)
    events, pending = find_retests(o, h, l, c, ctx.closes, levels, pip, tol_pips, max_wait)
    blocks = []
    for d in recent_trading_days(now, days + 3):  # extra days: a "previous" session + days without data yet
        for name, s, e in session_blocks(d):
            blocks.append((d, name, s, e))
    rows, prev = [], None
    for d, name, s, e in blocks:
        if s >= now:
            continue
        sub = df[(df.index >= s) & (df.index < e)]
        st = session_stats(sub, pip)
        if st is None:
            continue
        row = {"trading_day": d.isoformat(), "session": name, "start_utc": iso(s), "end_utc": iso(e),
               "in_progress": bool(e > now), **st}
        ph = pl = None
        if prev is not None:
            ph = extreme_status(sub["c"].to_numpy(float), sub["h"].to_numpy(float), prev["body_high"], "high")
            pl = extreme_status(sub["c"].to_numpy(float), sub["l"].to_numpy(float), prev["body_low"], "low")
            row["vs_prev_session"] = {"prev_session": f"{prev['session']} {prev['trading_day']}",
                                      "prev_body_high": prev["body_high"], "prev_body_low": prev["body_low"],
                                      "high": ph, "low": pl,
                                      "session_closed_beyond": "above" if st["close"] > prev["body_high"] else
                                      "below" if st["close"] < prev["body_low"] else "inside"}
        le = _line_events(events, ctx, s, e)
        mask = (df.index >= s) & (df.index < e)
        row["impulse_candles"] = int(ctx.imp[mask].sum())
        row["lines"] = le[-8:]
        row["lines_summary"] = {k: sum(x["event"] == k for x in le) for k in
                                ("broken_up", "broken_down", "retest_held", "retest_failed_closed_back_through")}
        row["character"] = character(st, ph, pl, row["lines_summary"]["retest_held"])
        rows.append(row)
        prev = row
    keep = sorted({r["trading_day"] for r in rows})[-days:]  # the last `days` trading days that have candles
    sessions = [r for r in rows if r["trading_day"] in keep]
    return {"sessions": sessions, "now": _now_state(ctx, events, pending, rows, pip, now, sl_pips, tp_pips, fx_like),
            "_rows": rows}


def _current_block(now: pd.Timestamp):
    """(current (name, start, end) or None, next (name, start))."""
    d0 = trade_date(now)
    cand = []
    for i in range(-1, 6):
        d = d0 + timedelta(days=i)
        if d.weekday() < 5:
            cand += session_blocks(d)
    cur = next((b for b in cand if b[1] <= now < b[2]), None)
    nxt = next(((b[0], b[1]) for b in cand if b[1] > now), None)
    return cur, nxt


def _now_state(ctx: Context, events, pending, rows, pip, now, sl_pips, tp_pips, fx_like) -> dict:
    df = ctx.df
    c = df["c"].to_numpy(float)
    n = len(df)
    last = float(c[-1])
    cur, nxt = _current_block(now)
    market_open = fx_market_open(now.to_pydatetime()) if fx_like else None
    state = {
        "now_utc": iso(now), "last_close": last, "last_candle_close_utc": iso(ctx.closes[-1]),
        "market_open": market_open,
        "current_session": cur[0] if cur and market_open is not False else None,
        "minutes_into_session": round((now - cur[1]).total_seconds() / 60) if cur and market_open is not False
        else None,
        "next_session": {"name": nxt[0], "open_utc": iso(nxt[1]),
                         "minutes_until": round((nxt[1] - now).total_seconds() / 60)} if nxt else None,
    }
    # today's developing range (trading day of `now`, or the last one with data)
    today = rows[-1]["trading_day"] if rows else None
    day_rows = [r for r in rows if r["trading_day"] == today]
    if day_rows:
        hi, lo = max(r["wick_high"] for r in day_rows), min(r["wick_low"] for r in day_rows)
        state["today"] = {"trading_day": today, "open": day_rows[0]["open"], "wick_high": hi, "wick_low": lo,
                          "body_high": max(r["body_high"] for r in day_rows),
                          "body_low": min(r["body_low"] for r in day_rows), "range_pips": (hi - lo) / pip,
                          "net_pips": (last - day_rows[0]["open"]) / pip,
                          "sessions_done": [r["session"] for r in day_rows if not r["in_progress"]]}
    # the user's lines around price
    lines = []
    for p in pending:
        lines.append({**p["lv"].public(), "status": "awaiting_break", "break_direction": "up" if p["d"] > 0 else "down"})
    for e in events:
        if e["touch_idx"] is None and not e["window_complete"]:
            lines.append({**e["lv"].public(), "status": "broken_awaiting_retest",
                          "break_direction": "up" if e["d"] > 0 else "down",
                          "broken_at": iso(ctx.closes[e["break_idx"]]), "candles_since_break": n - 1 - e["break_idx"]})
        elif e["touch_idx"] == n - 1:
            lines.append({**e["lv"].public(), "status": "retest_in_progress",
                          "break_direction": "up" if e["d"] > 0 else "down", "rejected": e["rejected"],
                          "wick_depth_pips": e.get("wick_depth_pips")})
    for x in lines:
        x["distance_pips"] = (x["level"] - last) / pip
    # one line per price (within 3 pips) and break direction: prefer the active status, then the higher timeframe
    rank = {"retest_in_progress": 0, "broken_awaiting_retest": 1, "awaiting_break": 2}
    lines.sort(key=lambda x: (rank[x["status"]], 0 if x["level_interval"] != ctx.interval else 1))
    uniq = []
    for x in lines:
        if not any(u["break_direction"] == x["break_direction"] and abs(u["level"] - x["level"]) <= 3 * pip
                   for u in uniq):
            uniq.append(x)
    lines = uniq
    above = sorted([x for x in lines if x["distance_pips"] > 0], key=lambda x: x["distance_pips"])[:4]
    below = sorted([x for x in lines if x["distance_pips"] <= 0], key=lambda x: -x["distance_pips"])[:4]
    state["lines_above"], state["lines_below"] = above, below
    # previous session extremes not yet traded through
    untouched = []
    done = [r for r in rows if not r["in_progress"]][-4:]
    for r in done:
        after = df[df.index >= pd.Timestamp(r["end_utc"])]
        hi_after = float(after["h"].max()) if len(after) else -np.inf
        lo_after = float(after["l"].min()) if len(after) else np.inf
        if hi_after < r["body_high"]:
            untouched.append({"session": f"{r['session']} {r['trading_day']}", "side": "high",
                              "body": r["body_high"], "wick": r["wick_high"],
                              "distance_pips": (r["body_high"] - last) / pip})
        if lo_after > r["body_low"]:
            untouched.append({"session": f"{r['session']} {r['trading_day']}", "side": "low", "body": r["body_low"],
                              "wick": r["wick_low"], "distance_pips": (r["body_low"] - last) / pip})
    state["untouched_session_extremes"] = sorted(untouched, key=lambda x: abs(x["distance_pips"]))
    state["next_actions"] = next_actions(above, below, state["untouched_session_extremes"], ctx.interval, pip,
                                         sl_pips, tp_pips, last, iso(ctx.closes[-1]))
    return state


def _px(p: float) -> str:
    a = abs(p)
    return f"{p:.5f}" if a < 10 else f"{p:.3f}" if a < 1000 else f"{p:.2f}"


def next_actions(above, below, untouched, interval, pip, sl_pips, tp_pips, last=None, last_close_time=None) -> list[str]:
    """If-then triggers in the break -> retest -> continue method. SL/TP are fixed pips from the ENTRY; the
    prices quoted for pending triggers assume a fill at the line."""
    acts = []

    def plan(line, d):
        P = line["level"]
        side, word, beyond, opp = ("long", "above", "below", "back below") if d > 0 else \
            ("short", "below", "above", "back above")
        sl, tp = P - d * sl_pips * pip, P + d * tp_pips * pip
        tag = f"{line['level_type']} {line['level_interval']} line {_px(P)}"
        risk = f"SL {sl_pips:g} pips {beyond} entry (≈{_px(sl)}), TP {tp_pips:g} pips (≈{_px(tp)})"
        if line["status"] == "awaiting_break":
            return (f"If a {interval} candle CLOSES {word} {_px(P)} ({tag}) → wait for the retest of {_px(P)} → "
                    f"{side} on a rejection close {word} it; {risk}. Wick through without a close = sweep, no trade.")
        if line["status"] == "broken_awaiting_retest":
            return (f"{tag} broken {'up' if d > 0 else 'down'} by close ({line['candles_since_break']} candles ago) → "
                    f"wait for the retest: {side} when a {interval} candle touches {_px(P)} and closes {word} it; "
                    f"{risk}. Invalid if a candle closes {opp} {_px(P)}.")
        if line.get("rejected"):
            e = last if last is not None else P
            ext = abs(e - P) / pip
            return (f"Retest HELD on the last closed {interval} candle ({(last_close_time or '')[5:16]}Z): touched "
                    f"{tag}, closed {word} it at {_px(e)} → {side} trigger; SL {_px(e - d * sl_pips * pip)}, TP "
                    f"{_px(e + d * tp_pips * pip)}"
                    + (f". Entry is already {ext:.0f} pips from the line (extended — chasing)." if ext > sl_pips else "."))
        return (f"Retest of {tag} in progress but the last candle closed {opp} it → no entry (break failed if it "
                f"stays {opp}).")
    order = {"retest_in_progress": 0, "broken_awaiting_retest": 1, "awaiting_break": 2}
    picks = sorted(above[:2] + below[:2], key=lambda x: (order[x["status"]], abs(x["distance_pips"])))
    for line in picks:
        acts.append(plan(line, 1 if line["break_direction"] == "up" else -1))
    for u in untouched[:2]:
        way, other = ("above", "below") if u["side"] == "high" else ("below", "above")
        acts.append(f"Untouched {u['session']} body {u['side']} {_px(u['body'])} ({u['distance_pips']:+.0f} pips): wick "
                    f"{way} + close back {other} = sweep (fade); {interval} close {way} = break → wait for the retest.")
    if not acts:
        acts.append("No body-level line near price: wait for a new swing to print before planning a break/retest.")
    return acts


# ------------------------------------------------------------------ service + markdown

def session_story(sym: Symbol, interval: str = "15m", days: int = 3, pip: Optional[float] = None,
                  sl_pips: float = 25, tp_pips: float = 75, level_interval: str = "1h") -> dict:
    now = now_ts()
    pip_used = pip or default_pip(sym)
    ctx = build_context(sym, interval, now, max_bars=6000)
    lvl_ctx = ctx if level_interval == interval else build_context(sym, level_interval, now, max_bars=6000)
    fx_like = sym.asset_class in ("fx", "metal")
    res = build_story(ctx, lvl_ctx, pip_used, days, now, sl_pips, tp_pips, fx_like=fx_like)
    res.pop("_rows", None)
    cal = {"timing_only": True, "events": [], "blackouts": []}
    try:
        c = get_calendar(list(sym.currencies) or ["USD"], ["High"], days=0.5, past_hours=0, now=now.to_pydatetime())
        for e in c["events"]:
            t = pd.Timestamp(e["time_utc"])
            cal["events"].append({"time_utc": e["time_utc"], "currency": e["currency"], "title": e["title"],
                                  "minutes_until": e["minutes_until"]})
            cal["blackouts"].append({"from_utc": iso(t - pd.Timedelta(minutes=BLACKOUT_MIN)),
                                     "to_utc": iso(t + pd.Timedelta(minutes=BLACKOUT_MIN)), "reason": e["title"]})
        cal["notes"] = c["notes"]
    except Exception as e:
        cal["error"] = str(e)[:200]
    res["now"]["calendar_next_12h"] = cal
    for b in cal["blackouts"]:
        res["now"]["next_actions"].append(f"No new entries {b['from_utc'][11:16]}–{b['to_utc'][11:16]} UTC "
                                          f"({b['reason']}): wait for the post-release candle close.")
    info = ctx.source_info or {"source": ctx.source}
    out = {
        "symbol": sym.id, "interval": interval, "level_interval": level_interval,
        "params": {"days": days, "pip": pip_used, "sl_pips": sl_pips, "tp_pips": tp_pips},
        **{k: v for k, v in info.items() if k != "basis_info"}, "as_of": iso(ctx.closes[-1]),
        "session_definition": "sequential blocks per NY trading day: Asia 17:00 NY(prev)→08:00 London; London "
                              "08:00 London→08:00 New York; New York 08:00→17:00 New York (DST-aware)",
        **res,
        "notes": [f"{sym.id} pip={pip_used:g}; break = candle CLOSE beyond a level, sweep = wick only. Closed "
                  f"{interval} candles only. Calendar is used for timing/blackouts only. Not financial advice."],
    }
    if info.get("basis_adjusted"):
        out["notes"].append("Gold/silver levels are in SPOT terms (futures candles minus the current basis "
                            f"{info.get('basis'):+.2f}); futures wicks can differ slightly from spot wicks.")
    try:  # lines & reactions across timeframes (shares the /levels cache)
        from .levels import levels
        ivs = list(dict.fromkeys(["4h", "1h", interval]))
        lv = levels(sym, ivs, pip_used)
        out["lines"] = {"zones_above": lv["zones_above"][:3], "zones_below": lv["zones_below"][:3],
                        "mtf_alignment": lv["mtf"]["alignment"], "odds": lv["now"]["odds"][:2],
                        "overall_hold_rate": lv["statistics"]["overall"].get("p_hold")}
    except Exception as e:
        out["lines"] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    out["markdown"] = story_markdown(out)
    return out


def story_markdown(s: dict) -> str:
    pip = s["params"]["pip"]
    L = [f"## {s['symbol']} session story ({s['interval']} candles, pip {pip:g}) — as of {s['now']['now_utc']}",
         f"_Source: {str(s.get('source')).split(' (')[0]}; last closed candle {s['as_of']}. Break = CLOSE beyond, "
         f"sweep = wick only. Sessions: Asia 17:00 NY→LDN open, London→NY open, New York→17:00 NY._",
         "", "| Day | Session | Open→Close | Net / Range (pips) | Body lo–hi | vs prev body hi / lo | Lines | Imp | "
         "Character |", "|---|---|---|---|---|---|---|---|---|"]
    for r in s["sessions"]:
        v = r.get("vs_prev_session") or {}
        ls = r["lines_summary"]
        lines = f"↑{ls['broken_up']} ↓{ls['broken_down']} held {ls['retest_held']} failed " \
                f"{ls['retest_failed_closed_back_through']}"
        L.append(f"| {r['trading_day'][5:]} | {r['session']}{'*' if r['in_progress'] else ''} | {_px(r['open'])}→"
                 f"{_px(r['close'])} | {r['net_pips']:+.0f} / {r['range_pips']:.0f} | {_px(r['body_low'])}–"
                 f"{_px(r['body_high'])} | {v.get('high', '-')} / {v.get('low', '-')} | {lines} | "
                 f"{r['impulse_candles']} | {r['character']} |")
    if any(r["in_progress"] for r in s["sessions"]):
        L.append("\\* in progress")
    n = s["now"]
    L.append("")
    nx = n.get("next_session") or {}
    if n.get("current_session"):
        L.append(f"**Now:** {n['current_session']} session, {n['minutes_into_session']} min in; next {nx.get('name')} "
                 f"in {nx.get('minutes_until')} min. Last close {_px(n['last_close'])}.")
    else:
        L.append(f"**Now:** market CLOSED (triggers below refer to the last closed candle); next {nx.get('name')} at "
                 f"{nx.get('open_utc')} ({nx.get('minutes_until')} min). Last close {_px(n['last_close'])}.")
    t = n.get("today")
    if t:
        L.append(f"Day {t['trading_day'][5:]} range: {_px(t['wick_low'])}–{_px(t['wick_high'])} "
                 f"({t['range_pips']:.0f} pips), net {t['net_pips']:+.0f} pips.")

    def fmt(x):
        return f"{_px(x['level'])} ({x['level_type']} {x['level_interval']}, {x['status'].replace('_', ' ')}, " \
               f"{x['distance_pips']:+.0f}p)"
    if n["lines_above"]:
        L.append("Lines above: " + "; ".join(fmt(x) for x in n["lines_above"][:2]))
    if n["lines_below"]:
        L.append("Lines below: " + "; ".join(fmt(x) for x in n["lines_below"][:2]))
    if n["untouched_session_extremes"]:
        L.append("Untouched session extremes: " + "; ".join(
            f"{u['session']} {u['side']} {_px(u['body'])} ({u['distance_pips']:+.0f}p)"
            for u in n["untouched_session_extremes"][:2]))
    cal = n.get("calendar_next_12h") or {}
    if cal.get("events"):
        L.append("High-impact (timing only, ±15 min blackout): " + "; ".join(
            f"{e['time_utc'][11:16]}Z {e['currency']} {e['title']}" for e in cal["events"][:5]))
    elif cal.get("notes"):
        L.append("Calendar: none listed in the next 12h (" + "; ".join(cal["notes"])[:160] + ")")
    ln = s.get("lines") or {}
    if ln.get("zones_above") is not None:
        L.append(f"\n**Lines & reactions** (MTF {ln['mtf_alignment']})")
        for z in list(reversed(ln["zones_above"][:2])) + ln["zones_below"][:2]:
            lr = z["last_reaction"]
            L.append(f"- {_px(z['level'])} {'+'.join(z['timeframes'])} {z['role']} ({z['distance_pips']:+.0f}p): "
                     f"{z['respected_touches']} respected, {z['flips']} flips"
                     + (f", last {lr['type']} {lr['time'][5:16]}Z" if lr else ", untested"))
        for o in ln["odds"][:1]:
            c1 = o["conditions"]["confluence"]
            if c1.get("p_hold") is not None:
                L.append(f"- Odds at {_px(o['level'])}: such zones ({c1['bucket']}) held {c1['p_hold'] * 100:.0f}% of "
                         f"touches (n={c1['n']})"
                         + (f" vs {c1['null_p_hold'] * 100:.0f}% on shuffled candles — "
                            f"{'different from chance' if c1.get('beats_null') else 'no better than chance'}"
                            if c1.get("null_p_hold") is not None else "") + ".")
    L.append("\n**Next actions**")
    for a in n["next_actions"][:4]:
        L.append(f"- {a}")
    return "\n".join(L)
