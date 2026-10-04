"""Analysis orchestration + the combined LLM-ready snapshot pack."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from . import indicators as ind
from .calendar import get_calendar
from .correlation import correlation_report
from .data import get_series, iso, norm_interval
from .model import predict
from .options import get_options
from .sessions import get_sessions
from .structure import analyze as structure_analyze
from .symbols import REGISTRY, Symbol


# ------------------------------------------------------------------ JSON helper

def jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (pd.Timestamp, datetime)):
        return iso(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        if not math.isfinite(f):
            return None
        return float(f"{f:.7g}")
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return [jsonable(v) for v in o.tolist()]
    return o


# ------------------------------------------------------------------ analysis

def analysis(sym: Symbol, interval: str = "1d") -> dict:
    interval = norm_interval(interval)
    s = get_series(sym, interval)
    daily = s.df if interval == "1d" else _try(lambda: get_series(sym, "1d").df)
    basis = ind.PIVOT_BASIS.get(interval, "1d")
    higher = _try(lambda: get_series(sym, basis).df)
    res = ind.analyze_frame(s.df, interval, daily=daily, higher=higher)
    return {"symbol": sym.id, "name": sym.name, "interval": interval, **s.source_info, "ticker": s.ticker,
            "as_of": s.as_of, "delayed_minutes": s.delayed_minutes, "bars": len(s.df), **res}


def _try(fn: Callable, default=None):
    try:
        return fn()
    except Exception:
        return default


# ------------------------------------------------------------------ snapshot

def _price_fmt(p: Optional[float]) -> str:
    if p is None or not isinstance(p, (int, float)) or not math.isfinite(p):
        return "n/a"
    a = abs(p)
    d = 5 if a < 10 else 3 if a < 1000 else 2
    return f"{p:,.{d}f}"


def _n(x, d=2, sign=False) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def _tf_summary(a: dict) -> dict:
    lv = a.get("levels", {})
    sup, res = lv.get("support") or [], lv.get("resistance") or []
    piv = a.get("pivots") or {}
    return {
        "interval": a["interval"], "last": a["last"], "last_time": a["last_time"], "source": a["source"],
        "change_pct_1bar": a["change_pct_1bar"], "trend": a["trend"]["regime"], "rsi14": a["rsi14"],
        "atr14": a["atr14"], "atr14_pct": a["atr14_pct"], "realized_vol_ann_pct": a["realized_vol_20_ann_pct"],
        "macd_hist": a["macd"]["hist"], "bb_pct_b": a["bollinger"]["pct_b"],
        "dist_atr": {k: a.get(f"dist_{k}_atr") for k in ("sma20", "sma50", "sma200")},
        "nearest_support": sup[:2], "nearest_resistance": res[:2],
        "pivots": {k: piv.get(k) for k in ("basis", "P", "R1", "S1", "R2", "S2")} if piv else None,
        "range_52w": a.get("range_52w"),
    }


def snapshot(sym: Symbol, timeframes: list[str], horizon: str = "1d", options_expiries: int = 3) -> dict:
    now = datetime.now(timezone.utc)
    tfs = [norm_interval(t) for t in timeframes][:5]
    related = [REGISTRY[r] for r in sym.related if r in REGISTRY][:6]
    tasks: dict[str, Callable] = {f"tf:{tf}": (lambda tf=tf: analysis(sym, tf)) for tf in tfs}
    if related:
        tasks["correlation"] = lambda: correlation_report([sym, *related], "1d", 60)
    tasks["calendar"] = lambda: get_calendar(list(sym.currencies) or ["USD"], ["High", "Medium"], days=3,
                                             past_hours=24)
    tasks["sessions"] = lambda: get_sessions(now)
    if sym.options_ticker:
        tasks["options"] = lambda: get_options(sym, options_expiries)
    tasks["prediction"] = lambda: predict(sym, None, horizon)
    for tf in tfs:
        if tf in ("15m", "30m", "1h", "4h", "1d"):
            tasks[f"structure:{tf}"] = (lambda tf=tf: structure_analyze(sym, tf, 300, with_sessions=(tf == "1h")))
    results, errors = {}, {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {k: ex.submit(fn) for k, fn in tasks.items()}
        for k, f in futs.items():
            try:
                results[k] = f.result(timeout=240)
            except Exception as e:
                errors[k] = f"{type(e).__name__}: {str(e)[:300]}"
    out: dict = {
        "symbol": sym.id, "name": sym.name, "asset_class": sym.asset_class, "generated_at": iso(now),
        "timeframes": {}, "errors": errors,
    }
    for tf in tfs:
        a = results.get(f"tf:{tf}")
        if a:
            out["timeframes"][tf] = _tf_summary(a)
    first = next((results[f"tf:{tf}"] for tf in tfs if f"tf:{tf}" in results), None)
    if first:
        out.update(source=first["source"], ticker=first["ticker"], as_of=first["as_of"],
                   delayed_minutes=first["delayed_minutes"], basis_adjusted=first.get("basis_adjusted", False))
        if first.get("basis_info"):
            out["basis_info"] = first["basis_info"]
    else:
        out.update(source=None, as_of=None)
    if "correlation" in results:
        c = results["correlation"]
        out["correlation"] = {"interval": c["interval"], "window": c["window"], "vs_base": c["vs_base"],
                              "errors": c["errors"]}
    if "calendar" in results:
        cal = results["calendar"]
        out["calendar"] = {k: cal[k] for k in ("source", "as_of", "filters", "events", "notes", "coverage_until")}
    if "sessions" in results:
        s = results["sessions"]
        out["sessions"] = {k: s[k] for k in ("now_utc", "fx_market_open", "active", "current_overlaps",
                                              "next_session")}
    if "options" in results:
        out["options"] = _options_summary(results["options"])
    if "prediction" in results:
        out["prediction"] = results["prediction"]
    st = {tf: _structure_summary(results[f"structure:{tf}"]) for tf in tfs if f"structure:{tf}" in results}
    if st:
        out["structure"] = st
    out = jsonable(out)
    out["markdown"] = render_markdown(out)
    return out


def _structure_summary(a: dict) -> dict:
    keep_box = ("status", "low", "high", "width_atr", "n_candles", "break_direction", "breakout_time",
                "breakout_impulse", "failed_time")
    return {"trend": a["trend"], "swings": [{k: s[k] for k in ("label", "body", "time", "session")} for s in a["swings"][-4:]],
            "last_bos": {k: a["last_bos"][k] for k in ("type", "level", "close", "close_time", "session_at_close",
                                                        "impulse")} if a.get("last_bos") else None,
            "box": {k: a["box"][k] for k in keep_box if k in a["box"]},
            "sweeps_48h": [{k: e[k] for k in ("type", "level", "wick", "close_time", "session_at_close")}
                           for e in a["sweeps_48h"][-3:]],
            "setup": a["setup"], "last_closed_candle_close_utc": a["last_closed_candle_close_utc"],
            "session_levels": a.get("session_levels")}


def _options_summary(o: dict) -> dict:
    e0 = o["expiries"][0]
    keep = ("expiry", "days_to_expiry", "put_call_oi_ratio", "put_call_volume_ratio", "max_pain", "atm_iv",
            "expected_move_1sd", "skew_put5_minus_call5", "net_gex_total", "gamma_flip_strike", "call_walls",
            "put_walls", "underlying_levels")
    return {"options_ticker": o["options_ticker"], "proxy": o["proxy"], "spot": o["spot"],
            "underlying_price": o["underlying_price"], "as_of": o["as_of"], "source": o["source"],
            "aggregate": o["aggregate"], "nearest_expiry": {k: e0.get(k) for k in keep},
            "other_expiries": [{k: e.get(k) for k in ("expiry", "days_to_expiry", "max_pain", "atm_iv",
                                                      "put_call_oi_ratio", "net_gex_total")}
                               for e in o["expiries"][1:]],
            "caveat": o["caveat"]}


# ------------------------------------------------------------------ markdown

def render_markdown(s: dict) -> str:
    L: list[str] = []
    sym = s["symbol"]
    L.append(f"## {sym} ({s.get('name')}) — quant snapshot @ {s['generated_at']}")
    lb = ", ".join(f"{tf} {str(t.get('last_time'))[:16]}Z" for tf, t in (s.get("timeframes") or {}).items())
    src = str(s.get("source") or "").split(" (")[0].split(", basis-adjusted")[0]
    L.append(f"_Prices: {src} `{s.get('ticker')}`; last bar start: {lb or s.get('as_of')}; est. feed "
             f"delay ~{s.get('delayed_minutes')}m. Daily bars stamped 00:00Z of trade date. "
             f"Analysis/probabilities only — not financial advice._")
    bi = s.get("basis_info")
    if s.get("basis_adjusted") and bi:
        L.append(f"**Levels are SPOT terms**: futures candles minus basis {bi['basis']:+.2f} (futures "
                 f"{_price_fmt(bi['futures_price'])} @ {str(bi['futures_as_of'])[:16]}Z − spot "
                 f"{_price_fmt(bi['spot_price'])} @ {str(bi['spot_as_of'])[:16]}Z, {bi['spot_source']}"
                 f"{', STALE' if bi.get('stale') else ''}). Older levels approximate (basis drifts with carry/roll).")
    ses = s.get("sessions")
    if ses:
        ov = ", ".join("+".join(x) for x in ses.get("current_overlaps") or []) or "none"
        nx = ses.get("next_session") or {}
        L.append(f"Sessions: FX market {'OPEN' if ses['fx_market_open'] else 'CLOSED'}; active: "
                 f"{', '.join(ses['active']) or 'none'}; overlap: {ov}; next: {nx.get('name')} {nx.get('open_utc')}")
    tfs = s.get("timeframes") or {}
    if tfs:
        L.append("\n### Technicals")
        L.append("| TF | Last | Chg% | SMA trend | RSI | ATR (%) | RV ann% | dist SMA20/50/200 (ATR) | Support (touches) | Resistance (touches) |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for tf, t in tfs.items():
            d = t["dist_atr"]
            sup = "; ".join(f"{_price_fmt(z['level'])} ({z['touches']})" for z in t["nearest_support"]) or "-"
            res = "; ".join(f"{_price_fmt(z['level'])} ({z['touches']})" for z in t["nearest_resistance"]) or "-"
            L.append(f"| {tf} | {_price_fmt(t['last'])} | {_n(t['change_pct_1bar'], 2, True)} | {t['trend']} | "
                     f"{_n(t['rsi14'], 0)} | {_price_fmt(t['atr14'])} ({_n(t['atr14_pct'], 2)}) | "
                     f"{_n(t['realized_vol_ann_pct'], 1)} | {_n(d['sma20'], 1, True)}/{_n(d['sma50'], 1, True)}/"
                     f"{_n(d['sma200'], 1, True)} | {sup} | {res} |")
        seen_basis = set()
        for tf, t in tfs.items():
            p = t.get("pivots")
            if p and p.get("P") and p["basis"] not in seen_basis:
                seen_basis.add(p["basis"])
                L.append(f"- pivots ({p['basis']}): S2 {_price_fmt(p['S2'])} S1 {_price_fmt(p['S1'])} "
                         f"P {_price_fmt(p['P'])} R1 {_price_fmt(p['R1'])} R2 {_price_fmt(p['R2'])}")
        L.append("- MACD hist / BB %b: " + ", ".join(f"{tf} {_n(t['macd_hist'], 4, True)} / {_n(t['bb_pct_b'], 2)}"
                                                  for tf, t in tfs.items()))
        r = next(iter(tfs.values())).get("range_52w")
        if r:
            L.append(f"- 52w range {_price_fmt(r['low'])}–{_price_fmt(r['high'])} (at {_n(r['position_pct'], 0)}%)")
    stc = s.get("structure")
    if stc:
        L.append("\n### Structure (Dow, body closes; closed candles only)")
        L.append("| TF | Trend | Last swings (body) | Last BOS (close, session) | Box (status, lo–hi, width ATR) | Triggers (close beyond) |")
        L.append("|---|---|---|---|---|---|")
        for tf, a in stc.items():
            sw = " ".join(f"{x['label']} {_price_fmt(x['body'])}" for x in a["swings"][-3:])
            b = a.get("last_bos")
            bos = (f"{b['type'].replace('bos_', '')} {_price_fmt(b['level'])} @ {b['close_time'][5:16]}Z "
                   f"{b['session_at_close']}{' IMPULSE' if b['impulse'] else ''}") if b else "-"
            bx = a["box"]
            box = (f"{bx['status']} {_price_fmt(bx['low'])}–{_price_fmt(bx['high'])} ({_n(bx['width_atr'], 1)} ATR, "
                   f"{bx['n_candles']} c)" + (f" brk {bx['break_direction']}" if bx.get("break_direction") else "")
                   ) if bx.get("status") not in (None, "none") else "none"
            se = a["setup"]
            L.append(f"| {tf} | {a['trend']} ({se['state']}) | {sw} | {bos} | {box} | "
                     f"↑{_price_fmt(se['long_trigger_level'])} / ↓{_price_fmt(se['short_trigger_level'])}; "
                     f"next close {se['next_candle_close_utc'][5:16]}Z |")
        for tf, a in stc.items():
            sws = (a.get("sweeps_48h") or [])[-2:] if tf in ("15m", "30m", "1h", "4h") else []
            if sws:
                L.append(f"- {tf} sweeps (48h): " + "; ".join(
                    f"{e['type'].replace('sweep_', '')} {_price_fmt(e['level'])} wick {_price_fmt(e['wick'])} "
                    f"{e['close_time'][5:16]}Z {e['session_at_close']}" for e in sws))
            sl = a.get("session_levels") or {}
            if sl.get("previous"):
                L.append("- prev sessions (body lo–hi | wick lo–hi): " + "; ".join(
                    f"{x['session']} {x['open_utc'][5:10]} {_price_fmt(x['body_low'])}–{_price_fmt(x['body_high'])} | "
                    f"{_price_fmt(x['wick_low'])}–{_price_fmt(x['wick_high'])}" for x in sl["previous"]))
    cor = s.get("correlation")
    if cor:
        L.append(f"\n### Correlation vs {sym} ({cor['interval']} returns)")
        L.append("| Asset | c20 | c60 | c250 | 1y avg c60 | beta | other(t-1)→base | regime chg |")
        L.append("|---|---|---|---|---|---|---|---|")
        for o, v in cor["vs_base"].items():
            L.append(f"| {o} | {_n(v['corr_20'])} | {_n(v['corr_60'])} | {_n(v['corr_250'])} | "
                     f"{_n(v['corr_60_avg_1y'])} | {_n(v['beta_first_on_other'])} | "
                     f"{_n(v['lead_other_t-1_vs_first_t'])} | {'YES' if v['regime_change'] else '-'} |")
    cal = s.get("calendar")
    if cal:
        cur = ",".join(cal["filters"]["currencies"]) if isinstance(cal["filters"]["currencies"], list) else "all"
        L.append(f"\n### Calendar ({cur}; High/Medium; last 24h + next 72h; {cal['source']}, fetched {cal['as_of']})")
        evs = cal["events"]
        if not evs:
            L.append("- none in window")
        for e in evs[:14]:
            when = f"in {_dur(e['minutes_until'])}" if e["status"] == "upcoming" else f"{_dur(-e['minutes_until'])} ago"
            fp = []
            if e.get("forecast"):
                fp.append(f"f {e['forecast']}")
            if e.get("previous"):
                fp.append(f"p {e['previous']}")
            L.append(f"- {e['time_utc'][:16]}Z {e['currency']} **{e['impact']}** {e['title']} ({when})"
                     + (f" [{', '.join(fp)}]" if fp else ""))
        if len(evs) > 14:
            L.append(f"- …{len(evs) - 14} more")
        for n in cal.get("notes") or []:
            L.append(f"- note: {n}")
    op = s.get("options")
    if op:
        px = op.get("proxy")
        ag, e = op["aggregate"], op["nearest_expiry"]
        hdr = f"{op['options_ticker']}" + (f" as {px['relation']} proxy" if px else "")
        L.append(f"\n### Options positioning ({hdr}; spot {_price_fmt(op['spot'])}; {op['source']}, OI = prior close)")
        L.append(f"- Aggregate (nearest expiries): net GEX ${_big(ag['net_gex_total'])}/1% → {ag['gamma_regime']}; "
                 f"gamma flip ≈ {_price_fmt(ag.get('gamma_flip_strike'))}"
                 + (f" (≈{_price_fmt(ag['gamma_flip_underlying'])} on {sym})" if ag.get("gamma_flip_underlying") else ""))
        cw = ", ".join(f"{_price_fmt(w['strike'])} ({w['oi']:,})" for w in e["call_walls"][:3])
        pw = ", ".join(f"{_price_fmt(w['strike'])} ({w['oi']:,})" for w in e["put_walls"][:3])
        L.append(f"- {e['expiry']} ({_n(e['days_to_expiry'], 1)}d): P/C OI {_n(e['put_call_oi_ratio'])}, "
                 f"max pain {_price_fmt(e['max_pain'])}, ATM IV {_n((e['atm_iv'] or float('nan')) * 100, 1)}%, "
                 f"1σ move ±{_price_fmt(e['expected_move_1sd'])}, skew(P5−C5) {_n((e['skew_put5_minus_call5'] if e['skew_put5_minus_call5'] is not None else float('nan')) * 100, 1, True)} vol pts")
        L.append(f"  - call walls: {cw or '-'}; put walls: {pw or '-'}")
        ul = e.get("underlying_levels")
        if ul:
            parts = [f"{k.replace('_', ' ')}: " + (", ".join(_price_fmt(x) for x in v[:3]) if isinstance(v, list)
                                                   else _price_fmt(v)) for k, v in ul.items() if k != "direction_note"]
            L.append(f"  - mapped to {sym}: " + "; ".join(parts))
        oth = op.get("other_expiries") or []
        if oth:
            L.append("- later: " + "; ".join(
                f"{x['expiry']} MP {_price_fmt(x['max_pain'])} IV {_n((x['atm_iv'] or float('nan')) * 100, 1)}% "
                f"P/C {_n(x['put_call_oi_ratio'])}" for x in oth))
        L.append(f"- caveat: {op['caveat']}")
    pr = s.get("prediction")
    if pr:
        L.append(f"\n### Model ({pr.get('engine')}; {pr['horizon_bars']}×{pr['interval']} ≈ {_n(pr['horizon_hours'], 0)}h ahead)")
        if pr.get("status") == "ok":
            q, rg, v = pr["expected_return_quantiles"], pr["expected_price_range"], pr["validation"]
            L.append(f"- P(up) = **{_n(pr['prob_up'], 3)}** (calibrated; raw {_n(pr.get('prob_up_raw'), 3)}) — "
                     f"{pr.get('signal_quality')}")
            L.append(f"- return q10/q50/q90 = {_n(q['q10'] * 100, 2, True)}% / "
                     f"{_n(q['q50'] * 100, 2, True)}% / {_n(q['q90'] * 100, 2, True)}% → price "
                     f"{_price_fmt(rg['q10'])} / {_price_fmt(rg['q50'])} / {_price_fmt(rg['q90'])}")
            L.append(f"- Walk-forward ({v.get('n_folds')} folds, {v.get('n_oos')} OOS of {v.get('n_samples')}): "
                     f"acc {_n(v.get('accuracy'), 3)} vs baseline {_n(v.get('baseline_accuracy'), 3)}; "
                     f"Brier {_n(v.get('brier'), 4)} vs {_n(v.get('baseline_brier'), 4)} (lower=better), "
                     f"BSS {_n(v.get('brier_skill_score'), 3, True)}")
            L.append("- top features: " + ", ".join(f["feature"] for f in pr["top_features"][:5]))
        else:
            L.append(f"- {pr.get('status')}: n_samples={pr.get('n_samples')}")
        L.append("- Price-pattern statistics only; prob near base rate = no signal; ignores scheduled news.")
    if s.get("errors"):
        L.append("\n### Section errors")
        for k, v in s["errors"].items():
            L.append(f"- {k}: {v[:160]}")
    return "\n".join(L)


def _dur(mins: Optional[float]) -> str:
    if mins is None:
        return "?"
    mins = abs(int(mins))
    if mins < 60:
        return f"{mins}m"
    if mins < 48 * 60:
        return f"{mins // 60}h{mins % 60:02d}m"
    return f"{mins / 1440:.1f}d"


def _big(x: Optional[float]) -> str:
    if x is None:
        return "n/a"
    a = abs(x)
    for div, suf in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{x / div:+.2f}{suf}"
    return f"{x:+.0f}"
