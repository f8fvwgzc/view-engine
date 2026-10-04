"""Retest lab: BREAK (body close through a swing-body level) -> RETEST (price returns to the level) -> continue.

Levels are swing BODY extremes (HH/HL/LH/LL by close) from the `level_interval` structure and, optionally, from
the entry interval itself. Everything is causal: a level exists only after its swing is confirmed, a break is the
first body close through it after that, and the retest is searched only in the `max_wait` candles after the break.

Entry variants:
  touch         limit order resting at the level, filled when a candle trades to it
  reject_close  close of the first candle that touched the level (± tol) and CLOSED back on the break side
  next_open     open of the candle after that rejection candle
Exits are fixed-pip stop/target; stop+target in one candle = loss; unresolved after k candles = marked to market.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import MIN_TRADES_FOR_VERDICT, verdict, wilson_ci
from .cache import TTL_INTRADAY, cache
from .data import BASIS_CAVEAT, default_history_days, iso, ns_index, replay_key
from .structure import _TN, Context, build_context, session_tag
from .symbols import Symbol

ENTRIES = ("touch", "reject_close", "next_open")
GRID_SL = [10, 15, 20, 25, 30, 40, 50, 60]
GRID_TP = [30, 50, 75, 100, 150]
PCTS = (50, 70, 80, 90, 95)
GRID_MIN_N = 50
SIGNAL_SL_PIPS, SIGNAL_TP_PIPS = 25.0, (50.0, 75.0, 100.0)


def default_pip(sym: Symbol) -> float:
    if sym.id == "XAUUSD":
        return 0.1
    if sym.id == "XAGUSD":
        return 0.01
    if sym.asset_class == "fx":
        return 0.01 if sym.id.endswith("JPY") else 0.0001
    if sym.asset_class in ("index", "commodity") and sym.id not in ("DXY", "VIX"):
        return 1.0 if sym.asset_class == "index" else 0.01
    return 0.01


def default_spread_price(sym: Symbol) -> float:
    """Typical retail spread in PRICE units (converted to pips with the pip actually used)."""
    if sym.id == "XAUUSD":
        return 0.25
    if sym.id == "XAGUSD":
        return 0.03
    if sym.asset_class == "fx":
        return 0.01 if sym.id.endswith("JPY") else 0.0001
    return 0.0


# ------------------------------------------------------------------ levels

@dataclass
class Level:
    price: float
    kind: str  # "H" (breaks upward) | "L" (breaks downward)
    label: str  # HH / LH / HL / LL / EQH / EQL / H / L
    time: pd.Timestamp  # swing candle start
    known_time: pd.Timestamp  # close of the candle that confirmed the swing
    interval: str

    def public(self) -> dict:
        return {"level": self.price, "level_type": self.label, "level_interval": self.interval,
                "level_time": iso(self.time), "level_known_at": iso(self.known_time),
                "level_session": "+".join(session_tag(self.time)["sessions"])}


def levels_from_context(ctx: Context) -> list[Level]:
    """Every swing body level as it became known in real time (uses run.history, not the pruned final list)."""
    out = []
    for s in ctx.run.history:
        out.append(Level(float(s.price), s.kind, s.label or s.kind, ctx.df.index[s.idx],
                         ctx.closes[s.confirmed_idx], ctx.interval))
    return out


# ------------------------------------------------------------------ break -> retest detection

def find_retests(o, h, l, c, closes: pd.DatetimeIndex, levels: list[Level], pip: float, tol_pips: float,
                 max_wait: int) -> tuple[list[dict], list[dict]]:
    """Returns (events, pending). events: one per level that was broken; pending: unbroken / waiting levels."""
    n = len(c)
    tol = tol_pips * pip
    closes = pd.DatetimeIndex(closes)
    events, pending = [], []
    for lv in levels:
        P = lv.price
        d = 1 if lv.kind == "H" else -1
        i0 = int(closes.searchsorted(lv.known_time, side="right"))  # first candle closing AFTER known
        start = max(i0, 1)
        if start >= n:
            pending.append({"lv": lv, "d": d, "status": "awaiting_break"})
            continue
        prev, cur = c[start - 1:n - 1], c[start:n]
        cross = (cur > P) & (prev <= P) if d > 0 else (cur < P) & (prev >= P)
        if not cross.any():
            if (c[-1] <= P) if d > 0 else (c[-1] >= P):
                pending.append({"lv": lv, "d": d, "status": "awaiting_break"})
            continue
        j = start + int(np.argmax(cross))
        lo, hi = j + 1, min(n, j + 1 + max_wait)
        ev = {"lv": lv, "d": d, "break_idx": j, "touch_idx": None, "fill_idx": None, "rejected": False,
              "window_complete": j + max_wait < n}
        if lo < hi:
            touch = (l[lo:hi] <= P + tol) if d > 0 else (h[lo:hi] >= P - tol)
            if touch.any():
                m = lo + int(np.argmax(touch))
                ev["touch_idx"] = m
                ev["rejected"] = bool(c[m] > P) if d > 0 else bool(c[m] < P)
                ev["wick_depth_pips"] = float((P - l[m]) / pip) if d > 0 else float((h[m] - P) / pip)
            fill = (l[lo:hi] <= P) if d > 0 else (h[lo:hi] >= P)
            if fill.any():
                ev["fill_idx"] = lo + int(np.argmax(fill))
        events.append(ev)
    return events, pending


# ------------------------------------------------------------------ fixed-pip simulation

def simulate_fixed(h, l, c, start: int, d: int, entry: float, sl: float, tp: float, k: int,
                   skip_target_on_first: bool = False) -> Optional[dict]:
    """Candles start .. start+k-1. sl/tp in PRICE units. None = still open (not enough candles yet)."""
    n = len(c)
    end = min(n, start + k)
    if start >= n:
        return None
    stop, target = entry - d * sl, entry + d * tp
    hs, ls = h[start:end], l[start:end]
    s_hit = (ls <= stop) if d > 0 else (hs >= stop)
    t_hit = ((hs >= target) if d > 0 else (ls <= target)).copy()
    if skip_target_on_first and len(t_hit):
        t_hit[0] = False  # the entry candle's far extreme may have printed before the fill
    i_s = int(np.argmax(s_hit)) if s_hit.any() else None
    i_t = int(np.argmax(t_hit)) if t_hit.any() else None
    if i_s is not None and (i_t is None or i_s <= i_t):
        return {"outcome": "loss", "move": -sl, "exit_idx": start + i_s}
    if i_t is not None:
        return {"outcome": "win", "move": tp, "exit_idx": start + i_t}
    if start + k > n:
        return None
    return {"outcome": "timeout", "move": float((c[end - 1] - entry) * d), "exit_idx": end - 1}


def entry_points(ev: dict, o, c, n: int) -> dict:
    """{variant: (start_idx, entry_price, skip_target_on_first)} for the variants that triggered."""
    out = {}
    if ev["fill_idx"] is not None:
        out["touch"] = (ev["fill_idx"], ev["lv"].price, True)
    m = ev["touch_idx"]
    if m is not None and ev["rejected"] and m + 1 < n:
        out["reject_close"] = (m + 1, float(c[m]), False)
        out["next_open"] = (m + 1, float(o[m + 1]), False)
    return out


def breakeven_fixed(sl_pips: float, tp_pips: float, spread_pips: float) -> float:
    """p*(tp - s) - (1-p)*(sl + s) = 0  ->  p = (sl + s) / (sl + tp)."""
    return min(1.0, (sl_pips + spread_pips) / (sl_pips + tp_pips))


def summarize_pips(trades: list[dict], sl_pips: float, tp_pips: float, spread_pips: float,
                   full: bool = False) -> dict:
    n = len(trades)
    be = breakeven_fixed(sl_pips, tp_pips, spread_pips)
    if n == 0:
        return {"n": 0, "win_rate": None, "win_rate_ci95": [None, None], "breakeven_win_rate": be,
                "expectancy_pips": None, "expectancy_r": None, "verdict": "insufficient data"}
    p = np.array([t["pips"] for t in trades], float)
    wins = int(sum(t["outcome"] == "win" for t in trades))
    lo, hi = wilson_ci(wins, n)
    exp = float(p.mean())
    out = {"n": n, "win_rate": wins / n, "win_rate_ci95": [lo, hi], "breakeven_win_rate": be,
           "expectancy_pips": exp, "expectancy_r": exp / sl_pips, "verdict": verdict(n, lo, be, exp)}
    if full:
        losses = int(sum(t["outcome"] == "loss" for t in trades))
        pos, neg = float(p[p > 0].sum()), float(-p[p < 0].sum())
        eq = np.concatenate([[0.0], np.cumsum(p)])
        out.update({"wins": wins, "losses": losses, "timeouts": n - wins - losses,
                    "profit_factor": pos / neg if neg > 0 else None, "total_pips": float(p.sum()),
                    "max_drawdown_pips": float((np.maximum.accumulate(eq) - eq).max())})
    return out


def _wick_bucket(depth: Optional[float], sl_pips: float) -> str:
    if depth is None:
        return "n/a"
    e = [round(x * sl_pips, 1) for x in (0.2, 0.4, 0.8)]
    if depth < 0:
        return "did not reach level (within tol)"
    if depth < e[0]:
        return f"0-{e[0]:g} pips"
    if depth < e[1]:
        return f"{e[0]:g}-{e[1]:g} pips"
    if depth < e[2]:
        return f"{e[1]:g}-{e[2]:g} pips"
    return f">={e[2]:g} pips"


def _session_bucket(ts) -> str:
    s = session_tag(ts)["sessions"]
    return "+".join(s) + (" (overlap)" if len(s) > 1 else "")


def build_trades(events: list[dict], o, h, l, c, closes, index, pip: float, sl_pips: float, tp_pips: float,
                 spread_pips: float, k: int, lvl_trend: Optional[np.ndarray] = None,
                 variants: tuple = ENTRIES) -> dict[str, list[dict]]:
    n = len(c)
    out: dict[str, list[dict]] = {v: [] for v in variants}
    seen: dict[str, set] = {v: set() for v in variants}
    for ev in sorted(events, key=lambda e: (e["touch_idx"] if e["touch_idx"] is not None else 10 ** 9)):
        d, lv = ev["d"], ev["lv"]
        for v, (start, entry, skip) in entry_points(ev, o, c, n).items():
            if v not in out or (start, d) in seen[v]:  # one trade per candle + direction
                continue
            sim = simulate_fixed(h, l, c, start, d, entry, sl_pips * pip, tp_pips * pip, k, skip)
            if sim is None:
                continue
            seen[v].add((start, d))
            t_entry = index[start] if v != "reject_close" else closes[start - 1]
            al = (lvl_trend[start] * d) if lvl_trend is not None else 0
            out[v].append({
                "entry_time": iso(t_entry), "direction": "long" if d > 0 else "short", "entry": entry,
                "stop": entry - d * sl_pips * pip, "target": entry + d * tp_pips * pip,
                "outcome": sim["outcome"], "pips": sim["move"] / pip - spread_pips,
                "exit_time": iso(closes[sim["exit_idx"]]), "bars_held": sim["exit_idx"] - start + 1,
                "break_time": iso(closes[ev["break_idx"]]), "wick_depth_pips": ev.get("wick_depth_pips"),
                **lv.public(),
                "session": _session_bucket(t_entry), "hour_utc": int(pd.Timestamp(t_entry).hour),
                "trend_alignment": "aligned" if al > 0 else "counter" if al < 0 else "neutral",
                "_start": start,
            })
    for v in out:
        out[v].sort(key=lambda t: t["_start"])
    return out


def _group(trades, keyfn, sl, tp, sp, order=None) -> dict:
    g: dict[str, list] = {}
    for t in trades:
        g.setdefault(str(keyfn(t)), []).append(t)
    keys = [x for x in (order or []) if x in g] + sorted((x for x in g if x not in (order or [])),
                                                         key=lambda x: (len(x), x))
    return {kk: summarize_pips(g[kk], sl, tp, sp) for kk in keys}


# ------------------------------------------------------------------ MAE / MFE from the level

def excursions(events: list[dict], h, l, pip: float, tp_pips: float, k: int) -> list[dict]:
    """Per retest (first touch within tol): adverse / favourable excursion measured FROM THE LEVEL.
    MAE runs from the touch candle until price first trades tp_pips beyond the level in the break direction
    (that candle included) or, if it never does, over the k-candle window."""
    n = len(h)
    out = []
    for ev in events:
        m = ev["touch_idx"]
        if m is None or m + k > n:  # need the complete window
            continue
        P, d = ev["lv"].price, ev["d"]
        hs, ls = h[m:m + k], l[m:m + k]
        fav = (hs - P) if d > 0 else (P - ls)
        adv = (P - ls) if d > 0 else (hs - P)
        reach = fav >= tp_pips * pip
        w = int(np.argmax(reach)) if reach.any() else None
        mae = float(adv[: (w + 1) if w is not None else k].max()) / pip
        out.append({"mae_pips": mae, "mfe_pips": float(fav.max()) / pip, "reached_tp": w is not None,
                    "bars_to_tp": (w + 1) if w is not None else None, "touch_idx": m})
    return out


def percentiles(values, pcts=PCTS) -> Optional[dict]:
    v = np.asarray(list(values), float)
    if len(v) == 0:
        return None
    return {f"p{p}": float(np.percentile(v, p)) for p in pcts}


def excursion_report(exc: list[dict], sl_grid=GRID_SL) -> dict:
    win = [e for e in exc if e["reached_tp"]]
    rep = {
        "measured_from": "the level (not the entry price); MAE = wick penetration beyond the level",
        "all_retests": {"n": len(exc), "wick_penetration_pips": percentiles(e["mae_pips"] for e in exc),
                        "max_favourable_pips": percentiles(e["mfe_pips"] for e in exc)},
        "reached_tp": {"n": len(win), "share_of_retests": len(win) / len(exc) if exc else None,
                       "wick_penetration_pips": percentiles(e["mae_pips"] for e in win),
                       "bars_to_tp": percentiles(e["bars_to_tp"] for e in win)},
    }
    if win:
        mae = np.array([e["mae_pips"] for e in win])
        rep["stop_survival"] = [{"stop_pips_beyond_level": s, "eventual_winners_kept": float((mae < s).mean())}
                                for s in sl_grid]
    return rep


# ------------------------------------------------------------------ orchestration

def retest_core(ctx: Context, lvl_ctx: Optional[Context], pip: float, sl_pips: float = 25, tp_pips: float = 75,
                spread_pips: float = 0.0, tol_pips: float = 3, max_wait: int = 48, k: int = 64,
                levels_mode: str = "both", grid_entry: str = "reject_close") -> dict:
    df = ctx.df
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    closes, index, n = ctx.closes, df.index, len(df)
    levels: list[Level] = []
    if lvl_ctx is not None:
        levels += levels_from_context(lvl_ctx)
    if levels_mode == "both" or lvl_ctx is None:
        if lvl_ctx is None or lvl_ctx.interval != ctx.interval:
            levels += levels_from_context(ctx)
    lvl_trend = None
    if lvl_ctx is not None:
        tr = pd.DataFrame({"t": ns_index(lvl_ctx.closes), "v": [_TN[x] for x in lvl_ctx.run.trend]})
        # trend known at the OPEN of each entry candle (= close of the previous one)
        lvl_trend = pd.merge_asof(pd.DataFrame({"t": ns_index(index)}), tr, on="t",
                                  direction="backward")["v"].fillna(0).to_numpy(float)
    events, pending = find_retests(o, h, l, c, closes, levels, pip, tol_pips, max_wait)
    trades = build_trades(events, o, h, l, c, closes, index, pip, sl_pips, tp_pips, spread_pips, k, lvl_trend)
    entries = {}
    for v, tl in trades.items():
        entries[v] = {
            "summary": summarize_pips(tl, sl_pips, tp_pips, spread_pips, full=True),
            "by_session": _group(tl, lambda t: t["session"], sl_pips, tp_pips, spread_pips),
            "by_level_type": _group(tl, lambda t: t["level_type"], sl_pips, tp_pips, spread_pips),
            "by_level_interval": _group(tl, lambda t: t["level_interval"], sl_pips, tp_pips, spread_pips),
            "by_trend_alignment": _group(tl, lambda t: t["trend_alignment"], sl_pips, tp_pips, spread_pips,
                                         ["aligned", "neutral", "counter"]),
            "by_hour_utc": _group(tl, lambda t: f"{t['hour_utc']:02d}", sl_pips, tp_pips, spread_pips),
            "by_wick_depth": _group(tl, lambda t: _wick_bucket(t["wick_depth_pips"], sl_pips), sl_pips, tp_pips,
                                    spread_pips),
            "recent_trades": [{kk: vv for kk, vv in t.items() if not kk.startswith("_")} for t in tl[-8:]],
        }
    exc = excursions(events, h, l, pip, tp_pips, k)
    # SL x TP grid for one entry variant
    cells = []
    for sl in GRID_SL:
        for tp in GRID_TP:
            tl = build_trades(events, o, h, l, c, closes, index, pip, sl, tp, spread_pips, k, None,
                              variants=(grid_entry,))[grid_entry]
            s = summarize_pips(tl, sl, tp, spread_pips)
            cells.append({"sl_pips": sl, "tp_pips": tp, "n": s["n"], "win_rate": s["win_rate"],
                          "breakeven_win_rate": s["breakeven_win_rate"], "expectancy_pips": s["expectancy_pips"],
                          "verdict": s["verdict"]})
    ok = [x for x in cells if x["n"] >= GRID_MIN_N and x["expectancy_pips"] is not None]
    best = max(ok, key=lambda x: x["expectancy_pips"]) if ok else None
    retested = [e for e in events if e["touch_idx"] is not None]
    funnel = {"levels": len(levels), "broken": len(events), "retested_within_max_wait": len(retested),
              "retest_rate": len(retested) / len(events) if events else None,
              "rejected_close_back_on_break_side": sum(e["rejected"] for e in retested),
              "closed_back_through_level": sum(not e["rejected"] for e in retested)}
    return {
        "funnel": funnel, "entries": entries, "excursions": excursion_report(exc),
        "grid": {"entry": grid_entry, "cells_tested": len(cells), "cells": cells, "best": best,
                 "warning": f"`best` is the maximum of {len(cells)} SL/TP combinations fitted on the same sample "
                            f"(multiple comparisons): it is optimistic/overfit by construction and needs "
                            f"out-of-sample confirmation. Only cells with n >= {GRID_MIN_N} are eligible."},
        "active": active_levels(events, pending, c, pip, n),
        "_events": events, "_levels": levels,
    }


def active_levels(events: list[dict], pending: list[dict], c, pip: float, n: int, limit: int = 12) -> list[dict]:
    last = float(c[-1])
    rows = []
    for p in pending:
        rows.append({**p["lv"].public(), "status": "awaiting_break",
                     "break_direction": "up" if p["d"] > 0 else "down"})
    for e in events:
        if e["touch_idx"] is None and not e["window_complete"]:
            rows.append({**e["lv"].public(), "status": "broken_awaiting_retest",
                         "break_direction": "up" if e["d"] > 0 else "down",
                         "candles_since_break": n - 1 - e["break_idx"]})
        elif e["touch_idx"] == n - 1:
            rows.append({**e["lv"].public(), "status": "retest_in_progress",
                         "break_direction": "up" if e["d"] > 0 else "down", "rejected": e["rejected"],
                         "wick_depth_pips": e.get("wick_depth_pips")})
    for r in rows:
        r["distance_pips"] = (r["level"] - last) / pip
    prio = {"retest_in_progress": 0, "broken_awaiting_retest": 1, "awaiting_break": 2}
    rows.sort(key=lambda r: (abs(r["distance_pips"])))
    near = rows[:limit]
    near.sort(key=lambda r: (prio[r["status"]], abs(r["distance_pips"])))
    return near


def retest(sym: Symbol, interval: str = "15m", level_interval: str = "1h", pip: Optional[float] = None,
           sl_pips: float = 25, tp_pips: float = 75, max_wait: int = 48, k: int = 64, tol_pips: float = 3,
           spread_pips: Optional[float] = None, levels: str = "both", grid_entry: str = "reject_close",
           history_days: Optional[int] = None) -> dict:
    if history_days is None:
        history_days = default_history_days(sym)
    if levels not in ("htf", "both"):
        raise ValueError("levels must be 'htf' or 'both'")
    if grid_entry not in ENTRIES:
        raise ValueError(f"grid_entry must be one of {ENTRIES}")
    pip_used = pip if pip else default_pip(sym)
    spread = spread_pips if spread_pips is not None else default_spread_price(sym) / pip_used

    def load():
        ctx = build_context(sym, interval, max_bars=1_000_000, history_days=history_days)
        lvl_ctx = ctx if level_interval == interval else build_context(sym, level_interval, max_bars=1_000_000,
                                                                       history_days=history_days)
        res = retest_core(ctx, lvl_ctx, pip_used, sl_pips, tp_pips, spread, tol_pips, max_wait, k, levels,
                          grid_entry)
        info = ctx.source_info or {"source": ctx.source}
        notes = [
            f"{sym.id} pip={pip_used:g}{' (default for this symbol)' if not pip else ''} → SL {sl_pips:g} pips = "
            f"{sl_pips * pip_used:g}, TP {tp_pips:g} pips = {tp_pips * pip_used:g}, tolerance {tol_pips:g} pips = "
            f"{tol_pips * pip_used:g} in price; spread {spread:g} pips"
            f"{' (default)' if spread_pips is None else ''} deducted from every trade.",
            "Break = first body close through a confirmed swing-body level (H levels break up, L levels down). "
            f"Retest = price back at the level (± tol) within {max_wait} candles. reject_close/next_open need the "
            "first touching candle to CLOSE on the break side; a close back through the level cancels them.",
            "touch = limit at the level; on the fill candle only the stop is checked (its high/low may predate "
            "the fill). Stop+target in the same candle = loss. One trade per entry candle + direction.",
            "Excursions are measured from the LEVEL: for a touch entry the wick penetration is the stop you needed; "
            "for reject_close/next_open add the distance between entry and level.",
            f"verdict = 'edge' only if the 95% Wilson CI lower bound > breakeven_win_rate, expectancy > 0 and "
            f"n >= {MIN_TRADES_FOR_VERDICT}. Trades overlap and sub-groups are many comparisons.",
            f"Data: {info.get('source')}; {len(ctx.df)} closed {interval} candles ({iso(ctx.df.index[0])} → "
            f"{iso(ctx.closes[-1])}). Yahoo intraday history is limited (5m/15m/30m ≈ 60 days, 1h ≈ 2 years), so "
            "samples are small; an OANDA token (OANDA_API_TOKEN) gives years of real spot candles. "
            "Not financial advice.",
        ]
        if info.get("basis_adjusted"):
            notes.append("Gold/silver candles are COMEX futures shifted by one constant basis to spot terms; pip "
                         "distances and outcomes are unaffected by the shift, but futures wicks differ slightly "
                         "from OTC spot wicks — which matters for wick-depth statistics. " + BASIS_CAVEAT)
        out = {
            "symbol": sym.id, "interval": interval, "level_interval": level_interval,
            "params": {"pip": pip_used, "sl_pips": sl_pips, "tp_pips": tp_pips, "spread_pips": spread,
                       "tol_pips": tol_pips, "max_wait": max_wait, "k": k, "levels": levels,
                       "grid_entry": grid_entry},
            **{x: v for x, v in info.items() if x != "basis_info"},
            "as_of": iso(ctx.closes[-1]), "last_close": float(ctx.df["c"].iloc[-1]), "candles": len(ctx.df),
            "period_start": iso(ctx.df.index[0]), "period_end": iso(ctx.closes[-1]),
            **{kk: vv for kk, vv in res.items() if not kk.startswith("_")}, "notes": notes,
        }
        out["markdown"] = retest_markdown(out)
        return out
    key = ("retest", sym.id, interval, level_interval, pip_used, sl_pips, tp_pips, max_wait, k, tol_pips, spread,
           levels, grid_entry, history_days, replay_key())
    return cache.get_or_set(key, TTL_INTRADAY, load)


# ------------------------------------------------------------------ live signals

def retest_signals(ctx: Context, lvl_ctx: Optional[Context], pip: float, tol_pips: float = 3,
                   max_wait: int = 48) -> list[dict]:
    """retest_long / retest_short fired by the LAST closed candle of ctx (touched a broken level and closed
    back on the break side)."""
    df = ctx.df
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    end = len(df) - 1
    levels = (levels_from_context(lvl_ctx) if lvl_ctx is not None and lvl_ctx.interval != ctx.interval else []) \
        + levels_from_context(ctx)
    events, _ = find_retests(o, h, l, c, ctx.closes, levels, pip, tol_pips, max_wait)
    out, seen = [], set()
    for e in events:
        if e["touch_idx"] != end or not e["rejected"] or e["d"] in seen:
            continue
        seen.add(e["d"])
        d = e["d"]
        entry = float(c[end])
        out.append({
            "type": "retest_long" if d > 0 else "retest_short", "direction": "up" if d > 0 else "down",
            **e["lv"].public(), "wick_depth_pips": e["wick_depth_pips"], "break_time": iso(ctx.closes[e["break_idx"]]),
            "candles_since_break": end - e["break_idx"], "pip": pip, "entry_ref": entry,
            "stop": entry - d * SIGNAL_SL_PIPS * pip, "stop_pips": SIGNAL_SL_PIPS,
            "targets": [{"kind": f"fixed_{int(tp)}_pips", "price": entry + d * tp * pip,
                         "r_multiple": round(tp / SIGNAL_SL_PIPS, 2)} for tp in SIGNAL_TP_PIPS],
        })
    return out


def _f(x, d=1, pct=False, sign=False):
    if x is None:
        return "n/a"
    if pct:
        return f"{x * 100:.0f}%"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def retest_markdown(r: dict) -> str:
    p = r["params"]
    L = [f"## {r['symbol']} retest lab — {r['interval']} entries on {r['level_interval']}"
         f"{'+' + r['interval'] if p['levels'] == 'both' else ''} body levels",
         f"_pip {p['pip']:g}; SL {p['sl_pips']:g} / TP {p['tp_pips']:g} pips; spread {p['spread_pips']:g}; retest "
         f"within {p['max_wait']} candles; exit within {p['k']}. {r['candles']} candles {r['period_start'][:10]}→"
         f"{r['period_end'][:10]}. Source: {str(r.get('source'))[:90]}._"]
    f = r["funnel"]
    L.append(f"Funnel: {f['broken']} breaks → {f['retested_within_max_wait']} retested ({_f(f['retest_rate'], pct=True)}) "
             f"→ {f['rejected_close_back_on_break_side']} held by close, {f['closed_back_through_level']} closed back "
             f"through.")
    L += ["", "| Entry | n | Win rate (95% CI) | Breakeven | Exp. pips | Exp. R | PF | Verdict |",
          "|---|---|---|---|---|---|---|---|"]
    for v, e in r["entries"].items():
        s = e["summary"]
        ci = s.get("win_rate_ci95") or [None, None]
        L.append(f"| {v} | {s['n']} | {_f(s['win_rate'], pct=True)} ({_f(ci[0], pct=True)}–{_f(ci[1], pct=True)}) | "
                 f"{_f(s['breakeven_win_rate'], pct=True)} | {_f(s['expectancy_pips'], 1, sign=True)} | "
                 f"{_f(s.get('expectancy_r'), 2, sign=True)} | {_f(s.get('profit_factor'), 2)} | {s['verdict']} |")
    x = r["excursions"]
    for name, key in (("all retests", "all_retests"), (f"retests that later ran ≥{p['tp_pips']:g} pips", "reached_tp")):
        w = x[key].get("wick_penetration_pips")
        if w:
            L.append(f"- Wick beyond the level, {name} (n={x[key]['n']}): " +
                     " / ".join(f"p{q} {w[f'p{q}']:.0f}" for q in PCTS) + " pips")
    if x.get("stop_survival"):
        L.append("- Stop beyond the level → share of eventual winners kept: " + ", ".join(
            f"{z['stop_pips_beyond_level']}p {_f(z['eventual_winners_kept'], pct=True)}" for z in x["stop_survival"]))
    g = r["grid"]
    b = g["best"]
    if b:
        L.append(f"- Best of {g['cells_tested']} SL×TP cells ({g['entry']}): SL {b['sl_pips']} / TP {b['tp_pips']} → "
                 f"n {b['n']}, win {_f(b['win_rate'], pct=True)} vs breakeven {_f(b['breakeven_win_rate'], pct=True)}, "
                 f"{_f(b['expectancy_pips'], 1, sign=True)} pips/trade, {b['verdict']}. "
                 f"⚠ picked after the fact from {g['cells_tested']} combinations — optimistic, needs out-of-sample.")
    else:
        L.append(f"- SL×TP grid ({g['entry']}): no cell with n ≥ {GRID_MIN_N}.")
    bs = r["entries"][g["entry"]]["by_session"]
    if bs:
        L += ["", f"| Session ({g['entry']}) | n | Win | Exp. pips | Verdict |", "|---|---|---|---|---|"]
        for kk, s in bs.items():
            L.append(f"| {kk} | {s['n']} | {_f(s['win_rate'], pct=True)} | {_f(s['expectancy_pips'], 1, sign=True)} | "
                     f"{s['verdict']} |")
    h = r.get("history")
    if h and not h.get("complete"):
        L.append(f"- History: asked for {h['requested_days']} days, Dukascopy cache holds {h['days_used']} contiguous "
                 f"day(s){' (feed is rate-limiting; backfill continues)' if h.get('rate_limited') else ''}.")
    L.append("- Verdict rule: CI lower bound > breakeven, expectancy > 0 and n ≥ 100. Statistics, not advice.")
    return "\n".join(L)
