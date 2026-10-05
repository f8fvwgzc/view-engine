"""Backtest of the body-close structure rules (same events, entries and stops as /signals).

Trade = one signal candle + direction. Entry at the signal candle's CLOSE, stop at the protected swing body
± 0.2 ATR, target = target_r × risk. Outcome within k candles: win (target first), loss (stop first; both in
the same candle = loss), timeout (marked to market in R at candle k). No look-ahead: every input to a signal
is known at its candle close (swings are only usable once confirmed).
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import pandas as pd

from .cache import TTL_INTRADAY, cache
from .data import BASIS_CAVEAT, default_history_days, iso, now_ts, replay_key, submit_ctx
from .model import boxes_for, event_features, structure_events
from .structure import FAIL_WINDOW, STOP_BUFFER_ATR, Context, build_context, session_tag
from .symbols import Symbol

MIN_TRADES_FOR_VERDICT = 100
Z95 = 1.959964
DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ------------------------------------------------------------------ statistics

def wilson_ci(wins: int, n: int, z: float = Z95) -> tuple[Optional[float], Optional[float]]:
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def breakeven_win_rate(target_r: float, cost_r: float) -> float:
    """Win rate at which expectancy is zero for pure win(+target_r)/loss(-1R) outcomes after costs."""
    return min(1.0, (1.0 + cost_r) / (1.0 + target_r))


def verdict(n: int, ci_low: Optional[float], breakeven: float, expectancy: Optional[float]) -> str:
    if n < MIN_TRADES_FOR_VERDICT:
        return "insufficient data"
    if ci_low is not None and ci_low > breakeven and (expectancy or 0) > 0:
        return "edge"
    return "no proven edge"


def summarize(trades: list[dict], target_r: float, cost_r: float, full: bool = False) -> dict:
    n = len(trades)
    be = breakeven_win_rate(target_r, cost_r)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "expectancy_r": None, "breakeven_win_rate": be,
                "verdict": "insufficient data"}
    net = np.array([t["r_net"] for t in trades], float)
    gross = np.array([t["r"] for t in trades], float)
    wins = int(sum(t["outcome"] == "win" for t in trades))
    lo, hi = wilson_ci(wins, n)
    out = {"n_trades": n, "win_rate": wins / n, "win_rate_ci95": [lo, hi], "breakeven_win_rate": be,
           "expectancy_r": float(net.mean()), "verdict": verdict(n, lo, be, float(net.mean()))}
    if not full:
        return out
    losses = int(sum(t["outcome"] == "loss" for t in trades))
    pos, neg = float(net[net > 0].sum()), float(-net[net < 0].sum())
    eq = np.cumsum(net)
    dd = float((np.maximum.accumulate(np.concatenate([[0.0], eq])) - np.concatenate([[0.0], eq])).max())
    streak = best = 0
    for x in net:
        streak = streak + 1 if x < 0 else 0
        best = max(best, streak)
    out.update({
        "wins": wins, "losses": losses, "timeouts": n - wins - losses,
        "avg_r": float(gross.mean()), "total_r": float(net.sum()),
        "profit_factor": (pos / neg) if neg > 0 else None, "max_drawdown_r": dd,
        "longest_losing_streak": int(best),
        "period_start": trades[0]["entry_time"], "period_end": trades[-1]["entry_time"],
    })
    return out


# ------------------------------------------------------------------ events -> trades

def failed_breakout_events(ctx: Context) -> list[dict]:
    """Close back inside a box within FAIL_WINDOW candles of a close outside it (mirrors box_status)."""
    c = ctx.df["c"].to_numpy(float)
    boxes = boxes_for(ctx)
    evs = []
    for t in range(2, len(c)):
        if boxes[t] is not None:  # some box still holding
            continue
        for j in range(1, FAIL_WINDOW + 1):
            e0 = t - j
            if e0 < 1:
                break
            b0 = boxes[e0]
            if not b0:
                continue
            bo = e0 + 1
            up_break = c[bo] > b0["high"]
            back = [x for x in range(bo + 1, t + 1) if b0["low"] <= c[x] <= b0["high"]]
            if back and back[0] == t:
                evs.append({"t": t, "type": "failed_breakout", "dir": -1 if up_break else 1,
                            "level": b0["high"] if up_break else b0["low"], "box": b0,
                            "failed_break_direction": "up" if up_break else "down"})
            break  # box_status stops at the most recent prior box
    return evs


def simulate_trade(h: np.ndarray, l: np.ndarray, c: np.ndarray, t: int, d: int, entry: float, stop: float,
                   risk: float, k: int, target_r: float) -> Optional[dict]:
    """Outcome using candles t+1 .. t+k only. None if the trade is still open (not enough future candles)."""
    tp = entry + d * target_r * risk
    n = len(c)
    for j in range(t + 1, min(n, t + k + 1)):
        hit_stop = l[j] <= stop if d > 0 else h[j] >= stop
        hit_tp = h[j] >= tp if d > 0 else l[j] <= tp
        if hit_stop:  # same-candle touch of both = loss
            return {"outcome": "loss", "r": -1.0, "exit_idx": j, "exit_price": stop}
        if hit_tp:
            return {"outcome": "win", "r": float(target_r), "exit_idx": j, "exit_price": tp}
    if t + k >= n:
        return None
    px = float(c[t + k])
    return {"outcome": "timeout", "r": float((px - entry) * d / risk), "exit_idx": t + k, "exit_price": px}


def _width_bucket(w: Optional[float]) -> str:
    if w is None or not np.isfinite(w):
        return "no box"
    return "<0.5 ATR" if w < 0.5 else "0.5-1 ATR" if w < 1 else "1-2 ATR" if w < 2 else ">=2 ATR"


def build_trades(ctx: Context, k: int = 24, target_r: float = 1.0, cost_r: float = 0.05,
                 include_failed: bool = True) -> list[dict]:
    df = ctx.df
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    evs = structure_events(ctx) + (failed_breakout_events(ctx) if include_failed else [])
    evs.sort(key=lambda e: (e["t"], e["type"]))
    merged: dict[tuple[int, int], dict] = {}
    for ev in evs:  # one trade per (candle, direction); remember every rule that fired
        key = (ev["t"], ev["dir"])
        if key in merged:
            merged[key]["types"].append(ev["type"])
            if ev.get("box") and not merged[key]["ev"].get("box"):
                merged[key]["ev"] = {**merged[key]["ev"], "box": ev["box"]}
        else:
            merged[key] = {"ev": dict(ev), "types": [ev["type"]]}
    trades = []
    for (t, d), m in sorted(merged.items()):
        fe = event_features(ctx, m["ev"])
        if fe is None:  # no protected swing yet / degenerate risk
            continue
        sim = simulate_trade(h, l, c, t, d, fe["entry"], fe["stop"], fe["risk"], k, target_r)
        if sim is None:
            continue
        close_t = ctx.closes[t]
        st = session_tag(close_t)
        align = ctx.htf_trend[t] * d
        box = m["ev"].get("box")
        atr = ctx.atr[t]
        trades.append({
            "entry_time": iso(close_t), "candle_time": iso(df.index[t]), "types": m["types"],
            "direction": "long" if d > 0 else "short", "entry": fe["entry"], "stop": fe["stop"],
            "target": fe["entry"] + d * target_r * fe["risk"], "risk": fe["risk"], "risk_atr": fe["risk"] / atr,
            "outcome": sim["outcome"], "r": sim["r"], "r_net": sim["r"] - cost_r,
            "exit_time": iso(ctx.closes[sim["exit_idx"]]), "bars_held": sim["exit_idx"] - t,
            "session": "+".join(st["sessions"]),
            "htf_alignment": "aligned" if align > 0 else "counter" if align < 0 else "neutral",
            "impulse": bool(ctx.imp[t]), "dow": DOW[close_t.dayofweek],
            "box_width_atr": (box["high"] - box["low"]) / atr if box else None,
            "_idx": t,
        })
    return trades


def _breakdown(trades: list[dict], keyfn, target_r: float, cost_r: float, order: Optional[list] = None) -> dict:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        ks = keyfn(t)
        for kk in (ks if isinstance(ks, list) else [ks]):
            groups.setdefault(str(kk), []).append(t)
    keys = [x for x in (order or []) if x in groups] + sorted(x for x in groups if x not in (order or []))
    return {kk: summarize(groups[kk], target_r, cost_r) for kk in keys}


def run_backtest(ctx: Context, k: int = 24, target_r: float = 1.0, cost_r: float = 0.05) -> dict:
    trades = build_trades(ctx, k, target_r, cost_r)
    overall = summarize(trades, target_r, cost_r, full=True)
    net = np.cumsum([t["r_net"] for t in trades]) if trades else np.array([])
    step = max(1, math.ceil(len(trades) / 200))
    idxs = sorted(set(list(range(step - 1, len(trades), step)) + ([len(trades) - 1] if trades else [])))
    curve = [{"t": trades[i]["entry_time"], "n": i + 1, "cum_r": round(float(net[i]), 3)} for i in idxs]
    public = lambda t: {x: v for x, v in t.items() if not x.startswith("_")}  # noqa: E731
    return {
        "overall": overall,
        "by_type": _breakdown(trades, lambda t: t["types"], target_r, cost_r),
        "by_session": _breakdown(trades, lambda t: t["session"], target_r, cost_r),
        "by_htf_alignment": _breakdown(trades, lambda t: t["htf_alignment"], target_r, cost_r,
                                       ["aligned", "neutral", "counter"]),
        "by_impulse": _breakdown(trades, lambda t: "true" if t["impulse"] else "false", target_r, cost_r),
        "by_day_of_week": _breakdown(trades, lambda t: t["dow"], target_r, cost_r, DOW),
        "by_box_width": _breakdown(trades, lambda t: _width_bucket(t["box_width_atr"]), target_r, cost_r,
                                   ["<0.5 ATR", "0.5-1 ATR", "1-2 ATR", ">=2 ATR", "no box"]),
        "equity_curve": curve,
        "recent_trades": [public(t) for t in trades[-20:]],
    }


# ------------------------------------------------------------------ service layer

def backtest(sym: Symbol, interval: str = "1h", k: int = 24, target_r: float = 1.0, cost_r: float = 0.05,
             history_days: Optional[int] = None) -> dict:
    if history_days is None:
        history_days = default_history_days(sym)

    def load():
        ctx = build_context(sym, interval, max_bars=60_000, history_days=history_days)
        res = run_backtest(ctx, k, target_r, cost_r)
        info = ctx.source_info or {"source": ctx.source}
        notes = [
            f"Costs: {cost_r}R deducted from every trade for spread/slippage (cost_r param); expectancy_r, total_r, "
            "profit_factor and drawdown are NET of it, avg_r is gross.",
            f"Rules: entry at signal-candle close; stop = protected swing body ± {STOP_BUFFER_ATR} ATR; target = "
            f"{target_r}R; exit within {k} candles (stop first / same-candle touch = loss; timeout marked to market). "
            "Stops are assumed filled at the stop price (gaps can lose more than 1R).",
            "One trade per signal candle + direction (rules firing together are merged; by_type counts a trade under "
            "each rule). Trades can overlap in time, so they are not independent and the Wilson CI is optimistic.",
            "verdict = 'edge' only if the 95% CI lower bound of win_rate > breakeven_win_rate, expectancy_r > 0 and "
            f"n >= {MIN_TRADES_FOR_VERDICT}. Sub-group verdicts are many comparisons — expect some false positives.",
            f"Data: {info.get('source')} ({len(ctx.df)} closed {interval} candles, "
            f"{iso(ctx.df.index[0])} → {iso(ctx.closes[-1])}). Past performance of a rule set is not a forecast; "
            "not financial advice.",
        ]
        if info.get("basis_adjusted"):
            notes.append("Gold/silver: futures candles shifted by one constant basis to spot terms — R outcomes are "
                         "unaffected by the shift, but COMEX futures wicks/sessions differ slightly from OTC spot. "
                         + BASIS_CAVEAT)
        return {"symbol": sym.id, "interval": interval, "params": {"k": k, "target_r": target_r, "cost_r": cost_r},
                **{x: v for x, v in info.items() if x != "basis_info"},
                "as_of": iso(ctx.closes[-1]), "candles": len(ctx.df), **res, "notes": notes}
    return cache.get_or_set(("backtest", sym.id, interval, k, target_r, cost_r, history_days, replay_key()),
                            TTL_INTRADAY * 5, load)


def backtest_matrix(symbols: list[Symbol], intervals: list[str], k: int = 24, target_r: float = 1.0,
                    cost_r: float = 0.05, history_days: Optional[int] = None) -> dict:
    cells = [(s, iv) for s in symbols for iv in intervals]

    def one(cell):
        s, iv = cell
        try:
            o = backtest(s, iv, k, target_r, cost_r, history_days)["overall"]
            return {"symbol": s.id, "interval": iv, "n_trades": o["n_trades"], "win_rate": o["win_rate"],
                    "win_rate_ci95": o.get("win_rate_ci95"), "expectancy_r": o["expectancy_r"],
                    "profit_factor": o.get("profit_factor"), "verdict": o["verdict"]}
        except Exception as e:
            return {"symbol": s.id, "interval": iv, "error": f"{type(e).__name__}: {str(e)[:200]}"}
    with ThreadPoolExecutor(max_workers=4) as ex:
        rows = [f.result() for f in [submit_ctx(ex, one, cell) for cell in cells]]
    return {"params": {"k": k, "target_r": target_r, "cost_r": cost_r},
            "breakeven_win_rate": breakeven_win_rate(target_r, cost_r), "rows": rows,
            "source": "see /backtest per cell", "as_of": iso(now_ts()),
            "notes": [f"expectancy_r is net of {cost_r}R cost per trade; verdict needs CI lower bound > breakeven "
                      f"and n >= {MIN_TRADES_FOR_VERDICT}. Not financial advice."]}
