"""Level reactions: horizontal lines at swing BODY levels, every reaction to them, and the measured odds.

Zones   swing body levels of each interval, merged when within 0.25 x ATR (lowest interval) of an existing
        zone. A zone remembers which timeframes formed it (confluence) and when each one became known.
Circles walking the LOWEST interval's closed candles, every interaction with a zone that was already known:
          rejection    touched, no close through, then moved >= 1 ATR away
          sweep        same, but a wick went beyond the zone (>= tol past the level) before closing back
          break        a body CLOSE beyond the zone
          retest_hold  first touch after a break that holds from the new side (support <-> resistance flip)
          retest_fail  first touch after a break that closes back through
        (stall = neither a close through nor a 1 ATR move within 16 candles; not counted in the odds)
Odds    P(hold) vs P(break) of a touch, conditioned only on what was known when the touch started.
No look-ahead: a level is usable only by candles that close after its swing was confirmed; the features of a
touch are frozen at its first candle; an earlier reaction counts only once it has resolved.
"""
from __future__ import annotations

import bisect
from functools import lru_cache
from statistics import median
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import wilson_ci
from .cache import TTL_INTRADAY, TTL_MODEL, cache
from .data import INTERVALS, default_history_days, iso, now_ts, ns_index, replay_key
from .retest import Level, default_pip, levels_from_context
from .structure import Context, bodies, box_status, build_context, is_fx_like, next_close_after
from .symbols import Symbol

ZONE_TOL_ATR = 0.25
MOVE_AWAY_ATR = 1.0
RESOLVE_WITHIN = 16  # candles to decide hold / break
FOLLOW_N = 16
LIFETIME_BARS = 200  # a level stays on the chart for this many bars of its OWN interval
MAX_CLOSES_THROUGH = 4  # after this many body closes through it, the zone is retired (chopped up)
MIN_RELIABLE_N = 30
HOLD_TYPES = ("rejection", "sweep", "retest_hold")
BREAK_TYPES = ("break", "retest_fail")


# ------------------------------------------------------------------ zones

def base_position(closes: pd.DatetimeIndex, t: pd.Timestamp) -> float:
    """Position of time `t` on the base-candle axis: index of the first candle closing after `t`; negative
    (estimated from the average candles-per-hour of the data) for times before the data starts."""
    if t >= closes[0]:
        return float(closes.searchsorted(t, side="right"))
    span_h = max((closes[-1] - closes[0]).total_seconds() / 3600, 1e-9)
    rate = (len(closes) - 1) / span_h if len(closes) > 1 else 1.0
    return -((closes[0] - t).total_seconds() / 3600) * rate


def build_zones(levels: list[Level], base_closes: pd.DatetimeIndex, base_atr: np.ndarray, base_interval: str,
                tol_atr: float = ZONE_TOL_ATR, arrays: Optional[tuple] = None) -> list[dict]:
    """Merge levels (processed in the order they became known) into zones. Causal: a level joins a zone only
    if that zone already existed, was still alive, and lies within tol at the time the level became known.
    Lifetimes are counted in BASE candles (so weekends / closed hours do not age a line)."""
    base_closes = ns_index(base_closes)
    finite = np.flatnonzero(np.isfinite(base_atr) & (base_atr > 0))
    first_atr = float(base_atr[finite[0]]) if len(finite) else float("nan")
    base_h = INTERVALS[base_interval][4]
    zones: list[dict] = []
    prices: list[float] = []  # sorted zone prices
    order: list[int] = []  # zone index for each entry of `prices`
    def retired(z: dict, pos: float) -> bool:
        """Was this zone already chopped through (MAX_CLOSES_THROUGH body closes) by candle `pos`?
        Evaluated on candles before `pos` only."""
        if arrays is None:
            return False
        if z.get("retired_pos") is not None:
            return z["retired_pos"] <= pos
        k = int(min(pos, len(base_closes)))
        if k < 2 or z.get("_checked", -1) >= k:
            return False
        o, h, l, c, atr_prev, imp, pip = arrays
        tmp = {**z, "expires_pos": min(z["expires_pos"], k)}
        rs = zone_reactions(tmp, o[:k], h[:k], l[:k], c[:k], base_closes[:k], atr_prev[:k], imp[:k], pip)
        if tmp.get("closes_through", 0) >= MAX_CLOSES_THROUGH:
            z["retired_pos"] = max(r["resolved_idx"] for r in rs if r["resolved_idx"] is not None) + 1
            return True
        z["_checked"] = k
        return False

    for lv in sorted(levels, key=lambda x: x.known_time):
        pos = base_position(base_closes, lv.known_time)
        i = int(pos) - 1
        atr = float(base_atr[i]) if 0 <= i < len(base_atr) and np.isfinite(base_atr[i]) and base_atr[i] > 0 \
            else first_atr
        if not np.isfinite(atr):
            continue
        tol = tol_atr * atr
        life = LIFETIME_BARS * INTERVALS[lv.interval][4] / base_h  # in base candles
        member = {"interval": lv.interval, "price": lv.price, "label": lv.label, "kind": lv.kind,
                  "time": lv.time, "known_time": lv.known_time, "known_pos": pos, "expires_pos": pos + life}
        lo = bisect.bisect_left(prices, lv.price - tol)
        hi = bisect.bisect_right(prices, lv.price + tol)
        best = None
        for k in range(lo, hi):
            z = zones[order[k]]
            if z["expires_pos"] >= pos and abs(z["price"] - lv.price) <= max(tol, z["tol"]) \
                    and not retired(z, pos):
                if best is None or abs(z["price"] - lv.price) < abs(best["price"] - lv.price):
                    best = z
        if best is not None:
            best["members"].append(member)
            best["expires_pos"] = max(best["expires_pos"], member["expires_pos"])
            best.pop("_checked", None)  # the window grew: re-check retirement next time
            continue
        z = {"id": len(zones), "price": float(lv.price), "tol": tol, "members": [member],
             "known_time": lv.known_time, "first_seen": lv.time, "expires_pos": member["expires_pos"]}
        zones.append(z)
        k = bisect.bisect_left(prices, lv.price)
        prices.insert(k, lv.price)
        order.insert(k, z["id"])
    return zones


def confluence_at(zone: dict, t: pd.Timestamp) -> list[str]:
    """Timeframes whose level was already known strictly before `t` (sorted high -> low)."""
    tfs = {m["interval"] for m in zone["members"] if m["known_time"] < t}
    return sorted(tfs, key=lambda x: -INTERVALS[x][4])


# ------------------------------------------------------------------ reactions (the circles)

def approach_type(o, c, imp, t: int, side: int, atr: float) -> str:
    """How price ARRIVED, using only candles before the touch candle: an impulse candle toward the zone in
    the previous two candles, or >= 2 ATR travelled toward it over the previous three."""
    for b in (t - 1, t - 2):
        if b >= 0 and imp[b] and side * (c[b] - o[b]) < 0:
            return "impulse"
    if t >= 4 and side * (c[t - 4] - c[t - 1]) >= 2.0 * atr:
        return "impulse"
    return "drift"


def zone_reactions(zone: dict, o, h, l, c, closes: pd.DatetimeIndex, atr_prev: np.ndarray, imp: np.ndarray,
                   pip: float) -> list[dict]:
    """All interactions of the base candles with one zone, in time order."""
    n = len(c)
    P, tol = zone["price"], zone["tol"]
    start = int(closes.searchsorted(zone["known_time"], side="right"))  # first candle closing after known
    stop = int(min(n, max(zone["expires_pos"], 0)))
    start = max(start, 1)
    if start >= min(stop, n):
        return []
    stop = min(stop, n)
    cand = start + np.flatnonzero((l[start:stop] <= P + tol) & (h[start:stop] >= P - tol))
    out: list[dict] = []
    cursor, pending_retest, closes_through, holds, flips = start, False, 0, 0, 0
    for t in cand:
        t = int(t)
        if t < cursor:
            continue
        if closes_through >= MAX_CLOSES_THROUGH:
            break
        # side price is coming from: last close outside the zone before t
        side = 0
        for b in range(t - 1, max(t - 60, -1), -1):
            if c[b] > P + tol:
                side = 1
                break
            if c[b] < P - tol:
                side = -1
                break
        if side == 0:
            continue
        atr = atr_prev[t]
        if not np.isfinite(atr) or atr <= 0:
            continue
        kind, j_res, depth = "stall", None, 0.0
        last = min(n, t + RESOLVE_WITHIN + 1)
        for j in range(t, last):
            far = l[j] if side > 0 else h[j]
            depth = max(depth, side * (P - far))
            if side * (c[j] - P) < -tol:  # body close beyond the zone
                kind, j_res = "break", j
                break
            near = h[j] if side > 0 else l[j]
            if j > t and side * (near - P) >= MOVE_AWAY_ATR * atr:
                kind, j_res = "hold", j
                break
        resolved = j_res is not None
        if not resolved and last - t <= RESOLVE_WITHIN:  # ran out of candles: still in progress
            kind = "in_progress"
        swept = depth >= tol
        if kind == "hold":
            typ = "retest_hold" if pending_retest else ("sweep" if swept else "rejection")
        elif kind == "break":
            typ = "retest_fail" if pending_retest else "break"
        else:
            typ = kind
        t_close = closes[t]
        approach = approach_type(o, c, imp, t, side, atr)
        rec = {
            "zone_id": zone["id"], "level": P, "type": typ, "idx": t, "resolved_idx": j_res,
            "time": t_close, "resolved_time": closes[j_res] if resolved else None,
            "role_tested": "support" if side > 0 else "resistance",
            "wick_depth_pips": depth / pip, "swept": bool(swept),
            # features frozen at the first touch candle
            "confluence": confluence_at(zone, t_close), "prior_holds": holds, "flipped": flips > 0,
            "is_retest": pending_retest, "approach": approach, "atr": float(atr),
            "follow_through_pips": None,
        }
        if kind == "hold":
            w = slice(t + 1, min(n, t + 1 + FOLLOW_N))
            if w.start < n:
                rec["follow_through_pips"] = float((side * ((h[w] if side > 0 else l[w]) - P)).max() / pip)
            holds += 1
            if pending_retest:
                flips += 1
                pending_retest = False
            cursor = j_res + 1
        elif kind == "break":
            w = slice(j_res + 1, min(n, j_res + 1 + FOLLOW_N))
            if w.start < n:
                rec["follow_through_pips"] = float((-side * ((l[w] if side > 0 else h[w]) - P)).max() / pip)
            closes_through += 1
            pending_retest = True
            cursor = j_res + 1
        else:
            cursor = last
        out.append(rec)
    zone.update(holds=holds, flips=flips, closes_through=closes_through, pending_retest=pending_retest)
    return out


# ------------------------------------------------------------------ statistics

def confluence_bucket(tfs: list[str], intervals_desc: list[str]) -> str:
    """'15m only' / '+1h' / '+4h' (highest timeframe present decides)."""
    base = intervals_desc[-1]
    top = next((iv for iv in intervals_desc if iv in tfs), base)
    return f"{base} only" if top == base else f"+{top}"


def touches_bucket(n: int) -> str:
    return str(n) if n < 3 else "3+"


@lru_cache(maxsize=50000)
def _block(ts_value: int) -> str:
    from .story import _current_block
    cur, _ = _current_block(pd.Timestamp(ts_value, tz="UTC"))
    return cur[0] if cur else "off-session"


def session_of(ts: pd.Timestamp) -> str:
    """Asia / London / New York block of the candle that STARTED the touch."""
    return _block(int(pd.Timestamp(ts).floor("15min").value))


def bucket_stats(eps: list[dict], overall_rate: Optional[float] = None) -> dict:
    n = len(eps)
    holds = [e for e in eps if e["type"] in HOLD_TYPES]
    brks = [e for e in eps if e["type"] in BREAK_TYPES]
    if n == 0:
        return {"n": 0, "p_hold": None, "p_break": None, "ci95_hold": [None, None], "unreliable": True}
    lo, hi = wilson_ci(len(holds), n)
    fh = [e["follow_through_pips"] for e in holds if e["follow_through_pips"] is not None]
    fb = [e["follow_through_pips"] for e in brks if e["follow_through_pips"] is not None]
    out = {"n": n, "p_hold": len(holds) / n, "p_break": len(brks) / n, "ci95_hold": [lo, hi],
           "median_follow_through_after_hold_pips": median(fh) if fh else None,
           "median_follow_through_after_break_pips": median(fb) if fb else None,
           "unreliable": n < MIN_RELIABLE_N,
           "ci_clear_of_50": bool(n >= MIN_RELIABLE_N and (lo > 0.5 or hi < 0.5))}
    if overall_rate is not None:
        out["differs_from_overall"] = bool(n >= MIN_RELIABLE_N and (lo > overall_rate or hi < overall_rate))
    return out


def group_stats(eps: list[dict], keyfn, order: Optional[list] = None, overall_rate: Optional[float] = None) -> dict:
    g: dict[str, list] = {}
    for e in eps:
        g.setdefault(str(keyfn(e)), []).append(e)
    keys = [k for k in (order or []) if k in g] + sorted(k for k in g if k not in (order or []))
    return {k: bucket_stats(g[k], overall_rate) for k in keys}


def statistics(episodes: list[dict], intervals_desc: list[str]) -> dict:
    eps = [e for e in episodes if e["type"] in HOLD_TYPES + BREAK_TYPES]
    overall = bucket_stats(eps)
    rate = overall["p_hold"]
    base = intervals_desc[-1]
    conf_order = [f"{base} only"] + [f"+{iv}" for iv in reversed(intervals_desc[:-1])]
    return {
        "definition": f"hold = moved >= {MOVE_AWAY_ATR:g} ATR away before any body close through the zone; break = "
                      f"body close through; decided within {RESOLVE_WITHIN} candles (stalls excluded). Features are "
                      "those known when the touch started.",
        "resolved_touches": len(eps), "stalls_excluded": sum(e["type"] == "stall" for e in episodes),
        "overall": overall,
        "by_confluence": group_stats(eps, lambda e: confluence_bucket(e["confluence"], intervals_desc), conf_order, rate),
        "by_prior_respected_touches": group_stats(eps, lambda e: touches_bucket(e["prior_holds"]),
                                                  ["0", "1", "2", "3+"], rate),
        "by_role_flip": group_stats(eps, lambda e: "flipped" if e["flipped"] else "never flipped",
                                    ["never flipped", "flipped"], rate),
        "by_touch_kind": group_stats(eps, lambda e: "retest after break" if e["is_retest"] else "fresh / repeat test",
                                     None, rate),
        "by_session": group_stats(eps, lambda e: session_of(e["time"] - pd.Timedelta(minutes=1)),
                                  ["Asia", "London", "New York"], rate),
        "by_approach": group_stats(eps, lambda e: e["approach"], ["drift", "impulse"], rate),
        "by_type": {t: sum(e["type"] == t for e in episodes)
                    for t in ("rejection", "sweep", "break", "retest_hold", "retest_fail", "stall")},
    }


DIMENSIONS = ("by_confluence", "by_prior_respected_touches", "by_role_flip", "by_touch_kind", "by_session",
              "by_approach")
NULL_Z = 2.58  # two-sided 99% (many buckets are compared)


def edges(stats: dict) -> list[dict]:
    """Buckets whose 95% CI for P(hold) excludes 50% (n >= 30), each with the verdict against the shuffled
    baseline when one is attached (`real_edge` = also clearly different from what random candles produce)."""
    out = []
    for dim in DIMENSIONS:
        for k, v in stats[dim].items():
            if v.get("ci_clear_of_50"):
                out.append({"dimension": dim[3:], "bucket": k, "n": v["n"], "p_hold": v["p_hold"],
                            "ci95_hold": v["ci95_hold"], "differs_from_overall": v.get("differs_from_overall"),
                            "null_p_hold": v.get("null_p_hold"), "vs_null_z": v.get("vs_null_z"),
                            "real_edge": bool(v.get("beats_null"))})
    return out


def shuffled_base(df: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Null data: the same candles (shape and return relative to the previous close) in a random order within
    each hour of the day. Keeps the volatility profile of the sessions, destroys any memory of price levels."""
    pc = df["c"].shift(1)
    rel = np.log(df[["o", "h", "l", "c"]].div(pc, axis=0)).to_numpy()[1:]
    hours = df.index.hour.to_numpy()[1:]
    rng = np.random.default_rng(seed)
    perm = np.arange(len(rel))
    for hr in np.unique(hours):
        idx = np.flatnonzero(hours == hr)
        perm[idx] = idx[rng.permutation(len(idx))]
    rel = rel[perm]
    cum = np.concatenate([[0.0], np.cumsum(rel[:, 3])])  # log close path
    prev = float(df["c"].iloc[0]) * np.exp(cum[:-1])
    out = pd.DataFrame(prev[:, None] * np.exp(rel), columns=["o", "h", "l", "c"], index=df.index[1:])
    out["v"] = 1.0
    return out


def contexts_from_base(df: pd.DataFrame, intervals_desc: list[str]) -> Optional[dict[str, Context]]:
    """Contexts for every interval rebuilt from base candles alone (closed higher-timeframe candles only)."""
    from .data import close_times, fx_daily_from_hourly, resample_ohlc
    from .structure import context_from_frame
    base_iv = intervals_desc[-1]
    end = close_times(df.index[-1:], base_iv)[0]
    out = {}
    for iv in intervals_desc:
        if iv == base_iv:
            f = df
        elif iv in ("15m", "30m", "1h"):
            f = resample_ohlc(df, {"15m": "15min", "30m": "30min", "1h": "1h"}[iv])
        elif iv == "4h":
            f = resample_ohlc(resample_ohlc(df, "1h"), "4h")
        elif iv == "1d":
            f = fx_daily_from_hourly(resample_ohlc(df, "1h"))
        else:
            return None
        cl = close_times(f.index, iv, "fx")
        keep = np.asarray(cl <= end)
        f, cl = f[keep], cl[keep]
        if len(f) < 30:
            return None
        out[iv] = context_from_frame(None, iv, f, cl)
    return out


def null_benchmark(base_df: pd.DataFrame, intervals_desc: list[str], pip: float, shuffles: int = 2) -> Optional[dict]:
    """Hold/break statistics of the SAME rules on shuffled candles (pooled over `shuffles` runs)."""
    eps: list[dict] = []
    for seed in range(shuffles):
        ctxs = contexts_from_base(shuffled_base(base_df, seed + 1), intervals_desc)
        if ctxs is None:
            return None
        b = ctxs[intervals_desc[-1]]
        res = analyze_levels(ctxs, pip, ns_index(b.closes)[-1], with_now=False)
        eps += res["_episodes"]
    st_ = statistics(eps, intervals_desc)
    return {"method": f"{shuffles} runs on the same candles re-ordered at random within each hour of the day "
                      "(session volatility kept, level memory destroyed)",
            "overall": st_["overall"], **{d: st_[d] for d in DIMENSIONS}}


def attach_null(stats: dict, null: Optional[dict]) -> None:
    """Add null_p_hold / vs_null_z / beats_null to every bucket (two-proportion z-test, |z| >= 2.58)."""
    if not null:
        return

    def cmp(b: dict, nb: Optional[dict]) -> None:
        if not nb or not nb.get("n") or not b.get("n"):
            return
        p1, n1, p2, n2 = b["p_hold"], b["n"], nb["p_hold"], nb["n"]
        pool = (p1 * n1 + p2 * n2) / (n1 + n2)
        se = (pool * (1 - pool) * (1 / n1 + 1 / n2)) ** 0.5
        z = (p1 - p2) / se if se > 0 else 0.0
        b.update(null_p_hold=p2, null_n=n2, vs_null_z=round(z, 2),
                 beats_null=bool(n1 >= MIN_RELIABLE_N and n2 >= MIN_RELIABLE_N and abs(z) >= NULL_Z))
    cmp(stats["overall"], null["overall"])
    for d in DIMENSIONS:
        for k, b in stats[d].items():
            cmp(b, null[d].get(k))
    stats["null_benchmark"] = {"method": null["method"], "overall_p_hold": null["overall"].get("p_hold"),
                               "overall_n": null["overall"].get("n"),
                               "rule": f"beats_null = |z| >= {NULL_Z} against the shuffled baseline and n >= "
                                       f"{MIN_RELIABLE_N} in both"}


# ------------------------------------------------------------------ zone summaries + score

def zone_score(z: dict, now: pd.Timestamp, intervals_desc: list[str]) -> dict:
    tfs = confluence_at(z, now + pd.Timedelta(seconds=1))
    weights = {iv: i + 1 for i, iv in enumerate(reversed(intervals_desc))}  # base 1, next 2, top 3
    conf = sum(weights.get(t, 1) for t in tfs)
    last_t = max([m["known_time"] for m in z["members"]] + [r["time"] for r in z.get("reactions", [])])
    age_days = max((now - last_t).total_seconds() / 86400, 0)
    rec = 2.0 * float(np.exp(-age_days / 3.0))
    parts = {"respected_touches": min(z.get("holds", 0), 5) * 1.0, "flips": min(z.get("flips", 0), 3) * 1.5,
             "confluence": float(conf), "recency": round(rec, 2),
             "closes_through_penalty": -0.75 * z.get("closes_through", 0)}
    return {"score": round(sum(parts.values()), 2), "components": parts}


def _px(p: float) -> str:
    a = abs(p)
    return f"{p:.5f}" if a < 10 else f"{p:.3f}" if a < 1000 else f"{p:.2f}"


def public_reaction(r: dict) -> dict:
    return {"type": r["type"], "time": iso(r["time"]), "session": session_of(r["time"] - pd.Timedelta(minutes=1)),
            "role_tested": r["role_tested"], "wick_depth_pips": r["wick_depth_pips"],
            "follow_through_pips": r["follow_through_pips"], "approach": r["approach"],
            "confluence_then": r["confluence"]}


def zone_public(z: dict, last: float, pip: float, now: pd.Timestamp, intervals_desc: list[str]) -> dict:
    tfs = confluence_at(z, now + pd.Timedelta(seconds=1))
    rs = z.get("reactions", [])
    sc = zone_score(z, now, intervals_desc)
    return {
        "zone_id": z["id"], "level": z["price"], "low": z["price"] - z["tol"], "high": z["price"] + z["tol"],
        "timeframes": tfs, "members": [{"interval": m["interval"], "type": m["label"], "price": m["price"],
                                        "time": iso(m["time"])} for m in z["members"] if m["known_time"] <= now],
        "first_seen": iso(z["first_seen"]),
        "role": "support" if last > z["price"] else "resistance", "distance_pips": (z["price"] - last) / pip,
        "respected_touches": z.get("holds", 0), "flips": z.get("flips", 0),
        "closes_through": z.get("closes_through", 0), "awaiting_retest": bool(z.get("pending_retest")),
        **sc, "last_reaction": public_reaction(rs[-1]) if rs else None,
        "reactions": [public_reaction(r) for r in rs[-6:]],
    }


# ------------------------------------------------------------------ multi-timeframe stack

def forming_candle(base_df: pd.DataFrame, base_closes: pd.DatetimeIndex, last_htf_close: pd.Timestamp,
                   next_close: pd.Timestamp, now: pd.Timestamp) -> Optional[dict]:
    """The higher-timeframe candle in progress, built from closed lower-timeframe candles."""
    mask = np.asarray(base_df.index >= last_htf_close) & np.asarray(base_closes <= now)
    sub = base_df[mask]
    if sub.empty:
        return None
    o, c = float(sub["o"].iloc[0]), float(sub["c"].iloc[-1])
    return {"start": iso(sub.index[0]), "closes_at": iso(next_close),
            "minutes_to_close": max(0, round((next_close - now).total_seconds() / 60)),
            "o": o, "h": float(sub["h"].max()), "l": float(sub["l"].min()), "c": c,
            "body_low": min(o, c), "body_high": max(o, c), "direction": "up" if c > o else "down" if c < o else "flat",
            "built_from_candles": int(len(sub))}


def forming_vs_lines(f: dict, zones: list[dict], interval: str) -> list[str]:
    """What the forming candle would confirm if it closed here."""
    notes = []
    for z in zones:
        P = z["level"]
        tag = f"{_px(P)} ({'+'.join(z['timeframes'])})"
        if f["o"] < P < f["c"]:
            notes.append(f"if the {interval} candle closes here it CONFIRMS a break above {tag}")
        elif f["o"] > P > f["c"]:
            notes.append(f"if the {interval} candle closes here it CONFIRMS a break below {tag}")
        elif f["h"] > P and max(f["o"], f["c"]) < P:
            notes.append(f"{interval} candle is only a WICK through {tag} from below so far (sweep unless it closes above)")
        elif f["l"] < P and min(f["o"], f["c"]) > P:
            notes.append(f"{interval} candle is only a WICK through {tag} from above so far (sweep unless it closes below)")
    return notes


def mtf_stack(ctxs: dict[str, Context], intervals_desc: list[str], zones_pub: list[dict], now: pd.Timestamp,
              fx_like: bool) -> dict:
    base = ctxs[intervals_desc[-1]]
    rows = []
    for iv in intervals_desc:
        ctx = ctxs[iv]
        df = ctx.df
        end = len(df) - 1
        last = float(df["c"].iloc[-1])
        bx = box_status(df, end, _b=bodies(df))
        box = {"status": bx["status"], "low": bx.get("low"), "high": bx.get("high"),
               "break_direction": bx.get("direction")} if bx.get("status") != "none" else {"status": "none"}
        mine = [z for z in zones_pub if iv in z["timeframes"]]
        above = min((z for z in mine if z["level"] > last), key=lambda z: z["level"], default=None)
        below = max((z for z in mine if z["level"] <= last), key=lambda z: z["level"], default=None)
        row = {"interval": iv, "trend": ctx.run.trend[-1], "last_closed_candle_close": iso(ctx.closes[-1]),
               "box": box, "decision_line_above": above["level"] if above else None,
               "decision_line_below": below["level"] if below else None, "forming": None, "forming_notes": []}
        if iv != intervals_desc[-1]:
            nxt = next_close_after(ctx.closes[-1], iv, now, fx_like)
            f = forming_candle(base.df, base.closes, ctx.closes[-1], nxt, now)
            if f and nxt - ctx.closes[-1] <= pd.Timedelta(hours=INTERVALS[iv][4]) * 1.01:
                row["forming"] = f
                row["forming_notes"] = forming_vs_lines(f, zones_pub, iv)[:3]
        rows.append(row)
    trends = {r["trend"] for r in rows}
    align = "all up" if trends == {"up"} else "all down" if trends == {"down"} else "mixed"
    return {"alignment": align, "timeframes": rows}


# ------------------------------------------------------------------ core + odds for "now"

def analyze_levels(ctxs: dict[str, Context], pip: float, now: Optional[pd.Timestamp] = None,
                   fx_like: bool = True, near_atr: float = 20.0, top_each_side: int = 4, with_now: bool = True,
                   null: Optional[dict] = None) -> dict:
    """Pure (no I/O). `ctxs` maps interval -> Context of CLOSED candles; the lowest interval is the base."""
    now = now or now_ts()
    intervals_desc = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
    base = ctxs[intervals_desc[-1]]
    df = base.df
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    closes = ns_index(base.closes)
    levels: list[Level] = []
    for iv in intervals_desc:
        levels += levels_from_context(ctxs[iv])
    zones = build_zones(levels, closes, base.atr, intervals_desc[-1],
                        arrays=(o, h, l, c, base.atr_prev, base.imp, pip))
    episodes: list[dict] = []
    for z in zones:
        z["reactions"] = zone_reactions(z, o, h, l, c, closes, base.atr_prev, base.imp, pip)
        episodes += z["reactions"]
    episodes.sort(key=lambda e: e["idx"])
    stats = statistics(episodes, intervals_desc)
    if not with_now:
        return {"statistics": stats, "_episodes": episodes, "_zones": zones}
    attach_null(stats, null)
    last = float(c[-1])
    atr_now = float(base.atr[-1])
    alive = [z for z in zones if z["expires_pos"] >= len(c) and z.get("closes_through", 0) < MAX_CLOSES_THROUGH
             and z["known_time"] <= now]
    pub = [zone_public(z, last, pip, now, intervals_desc) for z in alive
           if abs(z["price"] - last) <= near_atr * atr_now]

    def pick(side):
        s = sorted((z for z in pub if (z["level"] > last) == (side == "above")), key=lambda z: abs(z["distance_pips"]))
        near = s[:top_each_side // 2]  # the nearest lines always show ...
        strong = sorted(s[top_each_side // 2:], key=lambda z: -z["score"])[:top_each_side - len(near)]
        return sorted(near + strong, key=lambda z: abs(z["distance_pips"]))  # ... plus the strongest further out
    above, below = pick("above"), pick("below")
    top = above + below
    session_now = session_of(now)
    odds = [touch_odds(z, stats, intervals_desc, session_now) for z in
            sorted(top, key=lambda z: abs(z["distance_pips"]))[:4]]
    return {
        "intervals": intervals_desc, "base_interval": intervals_desc[-1], "last_close": last, "atr_base": atr_now,
        "zone_tolerance": ZONE_TOL_ATR * atr_now, "zones_total": len(zones), "zones_active": len(alive),
        "zones_above": above, "zones_below": below, "statistics": stats, "edges": edges(stats),
        "mtf": mtf_stack(ctxs, intervals_desc, top, now, fx_like),
        "now": {"now_utc": iso(now), "session": session_now, "odds": odds},
        "_zones": zones, "_episodes": episodes, "_n": len(c),
        "_last": (float(o[-1]), float(h[-1]), float(l[-1]), float(c[-1])), "_last_close_time": closes[-1],
    }


def _line(name: str, b: Optional[dict]) -> Optional[str]:
    if not b or not b.get("n"):
        return None
    lo, hi = b["ci95_hold"]
    flag = " — UNRELIABLE (n<30)" if b["unreliable"] else ""
    return f"{name}: held {b['p_hold'] * 100:.0f}% (95% CI {lo * 100:.0f}–{hi * 100:.0f}%, n={b['n']}){flag}"


def touch_odds(z: dict, stats: dict, intervals_desc: list[str], session: str) -> dict:
    """Conditional hold/break odds for a touch of this zone right now."""
    conf = confluence_bucket(z["timeframes"], intervals_desc)
    prior = touches_bucket(z["respected_touches"])
    flip = "flipped" if z["flips"] > 0 else "never flipped"
    kind = "retest after break" if z["awaiting_retest"] else "fresh / repeat test"
    picks = {"confluence": (conf, stats["by_confluence"].get(conf)),
             "prior_respected_touches": (prior, stats["by_prior_respected_touches"].get(prior)),
             "role_flip": (flip, stats["by_role_flip"].get(flip)),
             "touch_kind": (kind, stats["by_touch_kind"].get(kind)),
             "session": (session, stats["by_session"].get(session))}
    lines = [x for x in (_line(f"{dim.replace('_', ' ')} = {k}", b) for dim, (k, b) in picks.items()) if x]
    ov = stats["overall"]
    text = (f"Zone {_px(z['level'])} ({'+'.join(z['timeframes'])}, {z['role']}, {z['respected_touches']} respected "
            f"touch(es), {z['flips']} flip(s), {z['distance_pips']:+.0f} pips): " + "; ".join(lines)
            + (f". All touches: held {ov['p_hold'] * 100:.0f}% (n={ov['n']})." if ov.get("n") else ""))
    return {"zone_id": z["zone_id"], "level": z["level"], "role": z["role"], "distance_pips": z["distance_pips"],
            "conditions": {dim: {"bucket": k, **(b or {"n": 0})} for dim, (k, b) in picks.items()},
            "text": text}


# ------------------------------------------------------------------ service + markdown

def levels(sym: Symbol, intervals: Optional[list[str]] = None, pip: Optional[float] = None,
           history_days: Optional[int] = None) -> dict:
    ivs = list(dict.fromkeys(intervals or ["4h", "1h", "15m"]))
    if not 1 <= len(ivs) <= 4:
        raise ValueError("1 to 4 intervals")
    pip_used = pip or default_pip(sym)
    if history_days is None:
        history_days = default_history_days(sym)

    def load():
        now = now_ts()
        ctxs = {iv: build_context(sym, iv, now, max_bars=1_000_000, history_days=history_days) for iv in ivs}
        desc = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
        base_df = ctxs[desc[-1]].df
        rk = replay_key()
        null = None
        try:  # expensive (two more passes): cached for 6 hours per symbol / intervals / (replay) day
            null = cache.get_or_set(("levels_null", sym.id, tuple(desc), pip_used, history_days, rk[:10] if rk else None),
                                    TTL_MODEL, lambda: null_benchmark(base_df, desc, pip_used))
        except Exception as e:  # pragma: no cover
            null = None
        res = analyze_levels(ctxs, pip_used, now, fx_like=is_fx_like(sym), null=null)
        base = ctxs[res["base_interval"]]
        info = base.source_info or {"source": base.source}
        out = {
            "symbol": sym.id, "params": {"intervals": res["intervals"], "pip": pip_used,
                                         "zone_tol_atr": ZONE_TOL_ATR, "follow_candles": FOLLOW_N,
                                         "lifetime_bars_own_interval": LIFETIME_BARS},
            **{k: v for k, v in info.items() if k != "basis_info"},
            "as_of": iso(base.closes[-1]), "candles": len(base.df), "period_start": iso(base.df.index[0]),
            **{k: v for k, v in res.items() if not k.startswith("_")},
            "notes": [
                f"{sym.id} pip={pip_used:g}. Zones = swing body levels merged within {ZONE_TOL_ATR} ATR "
                f"({res['base_interval']}); a level lives {LIFETIME_BARS} bars of its own interval (trading time); a zone is retired "
                f"after {MAX_CLOSES_THROUGH} body closes through it.",
                "`null_p_hold` is the hold rate the same rules produce on shuffled candles: only buckets flagged "
                "`beats_null` behave differently from random price action. A bucket whose CI excludes 50% is NOT an "
                "edge by itself, because the hold and break thresholds are not symmetric.",
                "Odds describe how such zones behaved historically on this symbol; a hold needs a 1 ATR move away, "
                "which is not the same as a tradable profit after stop and spread (see /retest for that). Buckets with "
                "n < 30 are flagged unreliable; many buckets are compared, so a few will look good by chance.",
                f"Reactions before {iso(base.df.index[0])} are unknown (start of the {res['base_interval']} history), so "
                "touch counts of older higher-timeframe lines start from there. Not financial advice.",
            ],
        }
        out["markdown"] = levels_markdown(out)
        return out, res
    key = ("levels", sym.id, tuple(ivs), pip_used, history_days, replay_key())
    return cache.get_or_set(key, TTL_INTRADAY, load)[0]


def levels_engine(sym: Symbol, intervals: list[str], pip: Optional[float] = None) -> tuple[dict, dict]:
    """(public result, raw result with _zones/_episodes) — shares the /levels cache (used by /signals)."""
    ivs = list(dict.fromkeys(intervals))
    pip_used = pip or default_pip(sym)
    hd = default_history_days(sym)
    key = ("levels", sym.id, tuple(ivs), pip_used, hd, replay_key())
    if cache.get(key) is None:
        levels(sym, ivs, pip, hd)
    return cache.get(key)


def classify_last_candle(r: dict, zone: dict, last: tuple, last_idx: int) -> Optional[str]:
    """What the last closed candle did at the zone (None if it did not interact)."""
    _, hi, lo, cl = last
    P, tol = zone["price"], zone["tol"]
    if r["resolved_idx"] == last_idx and r["type"] in BREAK_TYPES:
        return r["type"]
    if not (lo <= P + tol and hi >= P - tol) or r["idx"] > last_idx:
        return None
    if r["resolved_idx"] is not None and r["resolved_idx"] < last_idx:
        return None  # that reaction is over and no new one started on this candle
    side = 1 if r["role_tested"] == "support" else -1
    if side * (cl - P) > 0:
        far = lo if side > 0 else hi
        if side * (P - far) >= tol:
            return "sweep_provisional"
        return "retest_holding_provisional" if r["is_retest"] else "touch_holding_provisional"
    return "inside_zone"


def last_candle_touches(raw: dict, pub: dict) -> list[dict]:
    """`level_touch` candidates: what the LAST closed base candle did at a top zone, with the historical odds."""
    top = {z["zone_id"]: z for z in pub["zones_above"] + pub["zones_below"]}
    odds = {o["zone_id"]: o for o in pub["now"]["odds"]}
    zones = {z["id"]: z for z in raw["_zones"]}
    last_idx = raw["_n"] - 1
    out = []
    for zid, zp in top.items():
        rs = zones[zid].get("reactions", [])
        if not rs:
            continue
        kind = classify_last_candle(rs[-1], zones[zid], raw["_last"], last_idx)
        if kind:
            out.append({"zone": zp, "reaction": kind, "raw": rs[-1], "odds": odds.get(zid)})
    return out


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def levels_markdown(r: dict) -> str:
    pip = r["params"]["pip"]
    L = [f"## {r['symbol']} lines & reactions ({'/'.join(r['intervals'])}; pip {pip:g}) — as of {r['now']['now_utc']}",
         f"_Last close {_px(r['last_close'])}; {r['candles']} {r['base_interval']} candles since {r['period_start'][:10]}; "
         f"zone = body level ± {r['zone_tolerance'] / pip:.0f} pips. Source: {str(r.get('source')).split(' (')[0][:60]}._",
         "", "| Zone | TFs | Role | Dist (pips) | Respected | Flips | Last reaction |", "|---|---|---|---|---|---|---|"]
    for z in list(reversed(r["zones_above"])) + r["zones_below"]:
        lr = z["last_reaction"]
        last = f"{lr['type']} {lr['time'][5:16]}Z {lr['session']}" if lr else "untested"
        L.append(f"| {_px(z['level'])} | {'+'.join(z['timeframes'])} | {z['role']} | {z['distance_pips']:+.0f} | "
                 f"{z['respected_touches']} | {z['flips']} | {last} |")
    m = r["mtf"]
    L.append(f"\n**MTF stack ({m['alignment']}):** " + "; ".join(
        f"{t['interval']} {t['trend']}"
        + (f", box {t['box']['status']} {_px(t['box']['low'])}–{_px(t['box']['high'])}" if t["box"]["status"] != "none" else "")
        + (f", lines ↑{_px(t['decision_line_above'])}" if t["decision_line_above"] else "")
        + (f" ↓{_px(t['decision_line_below'])}" if t["decision_line_below"] else "")
        for t in m["timeframes"]))
    for t in m["timeframes"]:
        f = t.get("forming")
        if f:
            L.append(f"- Forming {t['interval']}: body {_px(f['body_low'])}–{_px(f['body_high'])} ({f['direction']}), "
                     f"closes in {f['minutes_to_close']} min ({f['closes_at'][11:16]}Z)"
                     + ("; " + "; ".join(t["forming_notes"]) if t["forming_notes"] else "; no line inside its body."))
        elif t["interval"] != r["base_interval"]:
            L.append(f"- Forming {t['interval']}: none yet (last one closed {t['last_closed_candle_close'][5:16]}Z).")
    s = r["statistics"]
    ov = s["overall"]
    if ov.get("n"):
        L.append(f"\n**Hold vs break on a touch** (n={ov['n']}, held {_pct(ov['p_hold'])}; hold = 1 ATR away before a "
                 f"close through):")
        for name, key in (("confluence", "by_confluence"), ("prior respected touches", "by_prior_respected_touches"),
                          ("session", "by_session")):
            L.append(f"- by {name}: " + "; ".join(
                f"{k} {_pct(v['p_hold'])} ({_pct(v['ci95_hold'][0])}–{_pct(v['ci95_hold'][1])}, n={v['n']})"
                for k, v in s[key].items() if not v["unreliable"]))
        nb = s.get("null_benchmark")
        if nb:
            real = [(d[3:], k, v) for d in DIMENSIONS for k, v in s[d].items() if v.get("beats_null")]
            L.append(f"- Shuffled-candle baseline holds {_pct(nb['overall_p_hold'])} (n={nb['overall_n']}); real "
                     f"overall {_pct(ov['p_hold'])} (z {ov.get('vs_null_z', 0):+.1f}). Buckets that differ from random: "
                     + ("; ".join(f"{d}={k} {_pct(v['p_hold'])} vs {_pct(v['null_p_hold'])} random (z {v['vs_null_z']:+.1f}, "
                                  f"n={v['n']})" for d, k, v in real[:4]) if real else "none — lines hold about as "
                        "often as chance for these candles"))
    if r["now"]["odds"]:
        L.append("\n**If touched now** (historical share that held):")
        for o in r["now"]["odds"][:3]:
            c1, c2 = o["conditions"]["confluence"], o["conditions"]["prior_respected_touches"]
            L.append(f"- {_px(o['level'])} {o['role']} ({o['distance_pips']:+.0f}p): {_pct(c1.get('p_hold'))} for "
                     f"{c1['bucket']} zones (n={c1.get('n', 0)}), {_pct(c2.get('p_hold'))} with {c2['bucket']} prior "
                     f"respected touches (n={c2.get('n', 0)})"
                     + (f"; random baseline {_pct(c1.get('null_p_hold'))}" if c1.get("null_p_hold") is not None else ""))
    return "\n".join(L)
