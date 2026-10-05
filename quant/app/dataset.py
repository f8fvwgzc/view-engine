"""Chart-structure feature export with outcome labels (for the external calibrated decision model).

One row per CLOSED candle of the trigger interval. Every feature is scale-free (ATR units of the trigger
timeframe, ratios, counts, flags) so symbols can be pooled. No look-ahead: a row uses only candles that had
closed at its own close time; higher-timeframe state comes from completed higher-timeframe candles, plus the
forming higher-timeframe candle rebuilt from trigger candles up to that row.

/features returns the last row of the very same matrix, so it is identical to the /dataset row for that time.
"""
from __future__ import annotations

import bisect
import threading
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import events as EV
from . import mtf as M
from . import patterns as PT
from . import strength as ST
from .cache import TTL_INTRADAY, cache
from .data import INTERVALS, h4_offset, iso, now_ts, ns_index, replay_key
from .levels import MAX_CLOSES_THROUGH, build_zones, zone_reactions
from .retest import default_pip, default_spread_price, levels_from_context
from .structure import Context, build_context, is_fx_like
from .symbols import Symbol

FEATURE_VERSION = "structure-v3"
HIGHER = {"1min": ("5m", "15m"), "5m": ("15m", "1h"), "15m": ("1h", "4h"), "1h": ("4h", "1d"), "4h": ("1d", "1wk")}
PUBLIC_IV = {"1min": "1m"}  # the trigger interval name shown to callers
CTX_ROLES = {"d1": "1d", "w1": "1wk"}  # compact daily / weekly context for every trigger
CTX_FEATURES = ["swing_trend", "pullback_dir", "pullback_depth", "brk_dir", "brk_age", "brk_edge_dist_atr",
                "box_inside", "box_pos", "imp_dir"]
LABEL_MARGIN = 400  # extra trigger candles computed past a chunk so its pattern labels can see their outcome
WARMUP = 300
RATE_SAMPLE = 300  # candles used to estimate candles-per-hour (fixed, so truncating later data changes nothing)
AGE_CAP = 500.0
ROLES = ("tr", "ht1", "ht2")

TF_FEATURES = [
    ("regime_consolidation", "1 if the timeframe is consolidating (a body-swing box is the reference)"),
    ("regime_impulse_up", "1 if a fresh impulsive leg up (>=2.5 ATR at >=0.5 ATR/candle, ended <=8 candles ago)"),
    ("regime_impulse_down", "1 if a fresh impulsive leg down"),
    ("regime_trend_up", "1 if body swings trend up (higher high + higher low) and no box / fresh impulse"),
    ("regime_trend_down", "1 if body swings trend down (lower high + lower low)"),
    ("swing_trend", "+1 HH+HL, -1 LH+LL, 0 mixed (last confirmed body swings)"),
    ("last_high_label", "+1 if the last confirmed swing high is a HH, -1 if LH, 0 equal/unknown"),
    ("last_low_label", "+1 if the last confirmed swing low is a HL, -1 if LL, 0 equal/unknown"),
    ("last_swing_is_high", "1 if the most recent confirmed swing is a high, 0 if a low"),
    ("pullback_dir", "+1 uptrend intact and price pulled back below the last swing high without closing below the "
                     "last HL; -1 mirror for a downtrend; 0 otherwise (continuation after corrective retracement)"),
    ("pullback_depth", "retracement depth 0-1.5: 0 = at the last swing extreme, 1 = at the protected HL/LH"),
    ("dist_swing_high_atr", "(last swing-high body - close) / trigger ATR"),
    ("dist_swing_low_atr", "(close - last swing-low body) / trigger ATR"),
    ("box_exists", "1 if a box is the active reference on this timeframe"),
    ("box_inside", "1 if the last close of this timeframe is inside the box (0 = closed outside)"),
    ("box_pos", "position of price in the box, 0 = bottom, 1 = top (clipped to -1..2)"),
    ("box_height_atr", "box height / trigger ATR"),
    ("box_age", "candles of this timeframe since the box window started (capped 500)"),
    ("sweeps_above", "wicks above the box top that closed back inside, during this consolidation"),
    ("sweeps_below", "wicks below the box bottom that closed back inside"),
    ("fakeouts_above", "body closes above the box that were back inside within 3 candles, this consolidation"),
    ("fakeouts_below", "body closes below the box that were back inside within 3 candles"),
    ("brk_dir", "direction of the last body close outside a box (+1 up, -1 down, 0 none yet)"),
    ("brk_age", "candles of this timeframe since that close (capped 500)"),
    ("brk_edge_dist_atr", "(close - broken edge) x break direction / trigger ATR: small positive = at the retest"),
    ("imp_dir", "direction of the last impulse leg (+1/-1, 0 none)"),
    ("imp_size_atr", "size of that leg in ATR of this timeframe"),
    ("imp_age", "candles of this timeframe since that leg ended (0 = in progress, capped 500)"),
]
FORMING_FEATURES = [
    ("form_dir_atr", "forming higher-timeframe candle: (price - its open) / trigger ATR"),
    ("form_elapsed", "fraction of that candle's time elapsed (0-1)"),
    ("form_pos", "position of price in its high-low range so far (0-1)"),
]
GLOBAL_FEATURES = [
    ("hour_sin", "sin of the UTC hour of the candle close"), ("hour_cos", "cos of the UTC hour of the candle close"),
    ("dow", "day of week of the close (0 = Monday)"),
    ("sess_asia", "1 if Sydney or Tokyo session hours"), ("sess_london", "1 if London session hours"),
    ("sess_newyork", "1 if New York session hours"), ("sess_overlap", "1 if London and New York are both open"),
    ("mins_since_session_open", "minutes since the most recent session open among those active (null if none)"),
    ("atr_pips", "ATR14 of the trigger timeframe in pips"),
    ("atr_vs_20d_median", "ATR14 / its median over the previous 20 days"),
    ("sl_atr", "stop distance / ATR"), ("tp_atr", "target 1 distance / ATR"), ("tp2_atr", "target 2 distance / ATR"),
    ("body_signed", "(close - open) / candle range, -1..1"), ("upper_wick_frac", "upper wick / candle range"),
    ("lower_wick_frac", "lower wick / candle range"), ("close_pos", "close position in the candle range, 0-1"),
    ("range_atr", "candle range / ATR"),
    ("ret4_atr", "(close - close 4 candles ago) / ATR"), ("ret16_atr", "(close - close 16 candles ago) / ATR"),
    ("ret96_atr", "(close - close 96 candles ago) / ATR"),
    ("sup_dist_atr", "(close - nearest active zone at or below price) / ATR"),
    ("sup_touches", "respected touches (holds) of that zone so far"),
    ("sup_tfs", "number of timeframes that formed that zone"),
    ("res_dist_atr", "(nearest active zone above price - close) / ATR"),
    ("res_touches", "respected touches (holds) of that zone so far"),
    ("res_tfs", "number of timeframes that formed that zone"),
    ("align_up", "timeframes (trigger + 2 higher) whose regime points up"),
    ("align_down", "timeframes whose regime points down"),
]


NEWS_DOCS = {
    "news_mins_to_next": "minutes to the next scheduled high-impact event of the pair's currencies (capped 1440)",
    "news_mins_since_last": "minutes since the last one (capped 1440)",
    "news_today": "1 if the next or the last event falls on the same New York calendar day as the row",
    "news_within_60m": "1 if an event is within 60 minutes before or after the row",
    **{f"news_next_{t}": f"1 if the next event (within 24h) is of type {t}" for t in EV.TYPES},
    **{f"news_last_{t}": f"1 if the last event (within 24h) was of type {t}" for t in EV.TYPES},
}
STRENGTH_DOCS = {f"str_{k}_{w}": (f"{'base currency strength' if k == 'base' else 'quote currency strength' if k == 'quote' else 'base minus quote strength'}"
                                  f" over the last {w} (volatility-normalised average move against the other majors; "
                                  f"for gold: its own move / USD strength)")
                 for w in ST.WINDOWS for k in ("base", "quote", "diff")}


def feature_names() -> list[str]:
    names = [f"{r}_{n}" for r in ROLES for n, _ in TF_FEATURES]
    names += [f"{r}_{n}" for r in ROLES[1:] for n, _ in FORMING_FEATURES]
    names += [n for n, _ in GLOBAL_FEATURES]
    names += [f"{r}_{n}" for r in CTX_ROLES for n in CTX_FEATURES]
    return names + list(EV.NEWS_FEATURES) + list(ST.STRENGTH_FEATURES) + PT.pattern_feature_names()


def feature_docs() -> dict[str, str]:
    tf = dict(TF_FEATURES)
    d = {f"{r}_{n}": f"[{r}] {t}" for r in ROLES for n, t in TF_FEATURES}
    d.update({f"{r}_{n}": f"[{r}] {t}" for r in ROLES[1:] for n, t in FORMING_FEATURES})
    d.update(dict(GLOBAL_FEATURES))
    d.update({f"{r}_{n}": f"[{r} = {CTX_ROLES[r]} context] {tf[n]}" for r in CTX_ROLES for n in CTX_FEATURES})
    d.update(NEWS_DOCS)
    d.update(STRENGTH_DOCS)
    d.update(PT.pattern_feature_docs())
    return d


_SESSION = {"hour_sin", "hour_cos", "dow", "sess_asia", "sess_london", "sess_newyork", "sess_overlap",
            "mins_since_session_open"}
_VOL = {"atr_pips", "atr_vs_20d_median", "sl_atr", "tp_atr", "tp2_atr"}
_CANDLE = {"body_signed", "upper_wick_frac", "lower_wick_frac", "close_pos", "range_atr", "ret4_atr", "ret16_atr",
           "ret96_atr"}


def feature_groups() -> dict[str, str]:
    """Every feature name -> session | news | structure | zones | strength | volatility | candle | patterns."""
    out = {}
    pat = set(PT.pattern_feature_names())
    for n in feature_names():
        if n in pat:
            g = "patterns"
        elif n in _SESSION:
            g = "session"
        elif n.startswith("news_"):
            g = "news"
        elif n.startswith("str_"):
            g = "strength"
        elif n in _VOL:
            g = "volatility"
        elif n in _CANDLE:
            g = "candle"
        elif n.startswith(("sup_", "res_")):
            g = "zones"
        else:
            g = "structure"
        out[n] = g
    return out


PATTERN_LABELS = {
    "cont_after_pullback": "rows where the trigger timeframe is in a pullback of an intact trend (tr_pullback_dir != 0): "
                           "1 if a body closes beyond the last swing extreme in the trend direction before a body "
                           "closes beyond the protected swing, within `horizon` candles; else 0",
    "ht1_cont_after_pullback": "the same judged on the first higher timeframe's swings and closes (ht1_pullback_dir "
                               "!= 0), within `horizon` candles of that timeframe",
    "fakeout": "rows that are a body close outside the trigger-timeframe box: 1 if a body closes back inside within "
               "3 candles, else 0",
    "retest_then_continue": "rows 0-3 candles after a close-confirmed break that is still open: 1 if price comes back "
                            "to the broken edge and then moves tp_pips beyond it in the break direction before a body "
                            "closes back inside, within `horizon` candles; else 0",
}


# ------------------------------------------------------------------ per-timeframe causal arrays

_LAB = {"HH": 1.0, "LH": -1.0, "HL": 1.0, "LL": -1.0}
_TREND = {"up": 1.0, "down": -1.0}


def tf_state(ctx: Context) -> dict[str, np.ndarray]:
    """State of one timeframe AFTER each of its candles closed (uses swings confirmed by that close only)."""
    df = ctx.df
    n = len(df)
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    atr = ctx.atr
    st = M.box_states(ctx)
    hist = sorted(ctx.run.history, key=lambda s: (s.confirmed_idx, s.idx))
    hp = 0
    alt: list = []
    out = {k: np.full(n, np.nan) for k in ("h1", "l1", "hlab", "llab", "last_high", "imp_dir", "imp_size", "imp_age",
                                           "regime")}
    h1 = l1 = None
    comp = None  # last completed impulse leg (dir, size_atr, end_idx)
    for t in range(n):
        changed = False
        while hp < len(hist) and hist[hp].confirmed_idx <= t:
            s = hist[hp]
            hp += 1
            if alt and alt[-1].kind == s.kind:
                alt.pop()
            alt.append(s)
            changed = True
        if changed:
            h1 = next((x for x in reversed(alt) if x.kind == "H"), None)
            l1 = next((x for x in reversed(alt) if x.kind == "L"), None)
            comp = None
            for a, b in zip(reversed(alt[-9:-1]), reversed(alt[-8:])):
                nb, av = b.idx - a.idx, atr[a.idx]
                mv = b.price - a.price
                if nb > 0 and np.isfinite(av) and av > 0 and abs(mv) >= M.IMPULSE_MIN_ATR * av \
                        and abs(mv) / nb >= M.IMPULSE_SPEED_ATR * av:
                    comp = (1.0 if mv > 0 else -1.0, abs(mv) / av, b.idx)
                    break
        if h1 is not None:
            out["h1"][t], out["hlab"][t] = h1.price, _LAB.get(h1.label, 0.0)
        if l1 is not None:
            out["l1"][t], out["llab"][t] = l1.price, _LAB.get(l1.label, 0.0)
        idir, isize, iage, in_prog = 0.0, np.nan, np.nan, False
        if alt:
            out["last_high"][t] = 1.0 if alt[-1].kind == "H" else 0.0
            s = alt[-1]
            nb, av = t - s.idx, atr[s.idx]
            mv = c[t] - s.price
            if nb > 0 and np.isfinite(av) and av > 0 and abs(mv) >= M.IMPULSE_MIN_ATR * av \
                    and abs(mv) / nb >= M.IMPULSE_SPEED_ATR * av:
                idir, isize, iage, in_prog = (1.0 if mv > 0 else -1.0), abs(mv) / av, 0.0, True
        if not in_prog and comp is not None:
            idir, isize, iage = comp[0], comp[1], float(t - comp[2])
        out["imp_dir"][t], out["imp_size"][t], out["imp_age"][t] = idir, isize, iage
        status = st.status[t]
        fresh = idir != 0 and iage <= M.IMPULSE_FRESH
        tr = ctx.run.trend[t]
        if fresh and (status in (M.NONE, M.OUT_UP, M.OUT_DOWN) or in_prog):
            reg = 1 if idir > 0 else 2
        elif status != M.NONE:
            reg = 0
        elif tr == "up":
            reg = 3
        elif tr == "down":
            reg = 4
        else:
            reg = 0
        out["regime"][t] = reg
    out["trend"] = np.array([_TREND.get(x, 0.0) for x in ctx.run.trend])
    out["top"], out["bot"], out["status"] = st.top, st.bot, st.status.astype(float)
    out["box_age"] = np.where(st.start >= 0, np.arange(n) - st.start, np.nan)
    # sweeps / fakeouts counted inside the current consolidation (episode)
    tol = np.concatenate([[0.0], M.BREAK_TOL_ATR * np.nan_to_num(atr[:-1])])
    ptop, pbot = np.concatenate([[np.nan], st.top[:-1]]), np.concatenate([[np.nan], st.bot[:-1]])
    pin = np.concatenate([[False], st.status[:-1] == M.INSIDE])
    with np.errstate(invalid="ignore"):
        sw_up = pin & (h > ptop + tol) & (c <= ptop + tol)
        sw_dn = pin & (l < pbot - tol) & (c >= pbot - tol)
    fk_up, fk_dn = np.zeros(n), np.zeros(n)
    brk_dir, brk_idx, brk_edge = np.zeros(n), np.full(n, np.nan), np.full(n, np.nan)
    for b in st.breaks:
        if b["back_idx"] is not None and b["back_idx"] - b["idx"] <= M.FAKEOUT_MAX:
            (fk_up if b["side"] > 0 else fk_dn)[b["back_idx"]] += 1
    order = sorted(st.breaks, key=lambda b: b["idx"])
    for i, b in enumerate(order):
        end = order[i + 1]["idx"] if i + 1 < len(order) else n
        brk_dir[b["idx"]:end], brk_idx[b["idx"]:end], brk_edge[b["idx"]:end] = b["side"], b["idx"], b["edge"]
    ep = st.episode

    def per_episode(x: np.ndarray) -> np.ndarray:
        cs = np.cumsum(x.astype(float))
        first = np.r_[True, ep[1:] != ep[:-1]]
        base = np.maximum.accumulate(np.where(first, np.arange(n), 0))
        res = cs - (cs[base] - x[base])
        return np.where(ep >= 0, res, 0.0)
    out["sweeps_above"], out["sweeps_below"] = per_episode(sw_up), per_episode(sw_dn)
    out["fakeouts_above"], out["fakeouts_below"] = per_episode(fk_up), per_episode(fk_dn)
    out["brk_dir"], out["brk_age"], out["brk_edge"] = brk_dir, np.arange(n) - brk_idx, brk_edge
    out["breaks"] = st.breaks
    return out


def tf_block(state: dict, pos: np.ndarray, close: np.ndarray, atr_tr: np.ndarray, own_tf: bool) -> np.ndarray:
    """Map a timeframe's state onto the trigger rows (`pos` = index of its last closed candle per row, -1 = none)
    and express distances with the trigger close / trigger ATR."""
    ok = pos >= 0
    p = np.where(ok, pos, 0)

    def g(k):
        return np.where(ok, state[k][p], np.nan)
    reg = g("regime")
    h1, l1, top, bot, status = g("h1"), g("l1"), g("top"), g("bot"), g("status")
    trend = g("trend")
    with np.errstate(invalid="ignore", divide="ignore"):
        span = h1 - l1
        up = (trend > 0) & (close < h1) & (close > l1) & (span > 0)
        dn = (trend < 0) & (close > l1) & (close < h1) & (span > 0)
        depth = np.where(up, (h1 - close) / span, np.where(dn, (close - l1) / span, 0.0))
        height = top - bot
        box = np.isfinite(top) & (status != M.NONE)
        pos_box = np.where(box & (height > 0), np.clip((close - bot) / height, -1.0, 2.0), np.nan)
        bdir = g("brk_dir")
        edge = g("brk_edge")
        cols = [
            (reg == 0), (reg == 1), (reg == 2), (reg == 3), (reg == 4),
            trend, g("hlab"), g("llab"), g("last_high"),
            np.where(up, 1.0, np.where(dn, -1.0, 0.0)), np.clip(depth, 0.0, 1.5),
            (h1 - close) / atr_tr, (close - l1) / atr_tr,
            box, box & (status == M.INSIDE), pos_box, np.where(box, height / atr_tr, np.nan),
            np.minimum(g("box_age"), AGE_CAP),
            g("sweeps_above"), g("sweeps_below"), g("fakeouts_above"), g("fakeouts_below"),
            bdir, np.minimum(g("brk_age"), AGE_CAP), bdir * (close - edge) / atr_tr,
            g("imp_dir"), g("imp_size"), np.minimum(g("imp_age"), AGE_CAP),
        ]
    out = np.column_stack([np.asarray(x, float) for x in cols])
    out[~ok] = np.nan
    return out


# ------------------------------------------------------------------ forming higher-timeframe candle

def bin_starts(index: pd.DatetimeIndex, htf: str, h4_off: int = 1) -> tuple[pd.DatetimeIndex, pd.Timedelta]:
    """Start of the higher-timeframe candle each trigger candle belongs to (same grids as the candle series)."""
    dur = pd.Timedelta(hours=INTERVALS[htf][4])
    if htf in ("5m", "15m", "30m", "1h"):
        return index.floor({"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h"}[htf]), dur
    ny = index.tz_convert("America/New_York")
    if htf == "4h":
        local = ny.tz_localize(None) - pd.Timedelta(hours=h4_off)
        start = local.floor("4h") + pd.Timedelta(hours=h4_off)
    elif htf == "1wk":  # trading week starts Sunday 17:00 New York
        local = ny.tz_localize(None) + pd.Timedelta(hours=7)
        day = local.normalize()
        start = day - pd.to_timedelta(day.dayofweek, unit="D") - pd.Timedelta(hours=7)
    else:  # 1d: trading day rolls at 17:00 New York
        local = ny.tz_localize(None) + pd.Timedelta(hours=7)
        start = local.normalize() - pd.Timedelta(hours=7)
    st = pd.DatetimeIndex(start).tz_localize("America/New_York", ambiguous="NaT", nonexistent="shift_forward")
    return st.tz_convert("UTC"), dur


def forming_block(df: pd.DataFrame, closes: pd.DatetimeIndex, atr: np.ndarray, htf: str, h4_off: int) -> tuple:
    start, dur = bin_starts(df.index, htf, h4_off)
    isnat = np.asarray(start.isna())
    start = pd.DatetimeIndex(start).as_unit("ns")
    key = pd.Series(start.asi8, index=df.index)
    g = df.groupby(key.to_numpy())
    f_open = g["o"].transform("first").to_numpy(float)
    f_high = g["h"].cummax().to_numpy(float)
    f_low = g["l"].cummin().to_numpy(float)
    c = df["c"].to_numpy(float)
    rng = f_high - f_low
    elapsed = (ns_index(closes).asi8 - start.asi8) / dur.value
    with np.errstate(invalid="ignore", divide="ignore"):
        block = np.column_stack([(c - f_open) / atr, np.clip(elapsed, 0.0, 1.0),
                                 np.where(rng > 0, (c - f_low) / rng, 0.5)])
    block[isnat] = np.nan
    return block, {"open": f_open, "high": f_high, "low": f_low, "start": start, "dur": dur}


# ------------------------------------------------------------------ time / sessions

_SESS = (("Sydney", "Australia/Sydney", 7, 16), ("Tokyo", "Asia/Tokyo", 9, 18), ("London", "Europe/London", 8, 17),
         ("New York", "America/New_York", 8, 17))


def time_block(closes: pd.DatetimeIndex) -> tuple[np.ndarray, dict]:
    t = ns_index(closes) - pd.Timedelta(seconds=1)  # the moment just before the close belongs to this candle
    hour = closes.hour + closes.minute / 60.0
    act, mins = {}, {}
    for name, tz, o, c in _SESS:
        loc = t.tz_convert(tz)
        a = (loc.dayofweek < 5) & (loc.hour >= o) & (loc.hour < c)
        act[name] = np.asarray(a)
        mins[name] = np.where(a, (loc.hour - o) * 60 + loc.minute + 1, np.inf)
    asia = act["Sydney"] | act["Tokyo"]
    m = np.minimum.reduce([mins[k] for k in mins])
    block = np.column_stack([np.sin(2 * np.pi * hour / 24), np.cos(2 * np.pi * hour / 24),
                             np.asarray(closes.dayofweek, float), asia, act["London"], act["New York"],
                             act["London"] & act["New York"], np.where(np.isfinite(m), m, np.nan)]).astype(float)
    return block, act


# ------------------------------------------------------------------ zones (levels code), event-driven

def zone_block(ctxs: dict[str, Context], roles: dict[str, str], pip: float) -> tuple[np.ndarray, dict]:
    base = ctxs[roles["tr"]]
    df = base.df
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    closes = ns_index(base.closes)
    n = len(df)
    k = min(n, RATE_SAMPLE)
    span_h = max((closes[k - 1] - closes[0]).total_seconds() / 3600, 1e-9)
    rate = (k - 1) / span_h if k > 1 else 1.0
    levels = []
    for iv in dict.fromkeys(roles.values()):
        if iv in ctxs:
            levels += levels_from_context(ctxs[iv])
    zones = build_zones(levels, closes, base.atr, roles["tr"], arrays=(o, h, l, c, base.atr_prev, base.imp, pip),
                        rate=rate)
    add, rem, hold, join = {}, {}, {}, {}
    for z in zones:
        rs = zone_reactions(z, o, h, l, c, closes, base.atr_prev, base.imp, pip, resume=True)
        dead = z["expires_pos"]
        thru = [r["resolved_idx"] for r in rs if r["type"] in ("break", "retest_fail")]
        if len(thru) >= MAX_CLOSES_THROUGH:
            dead = min(dead, thru[MAX_CLOSES_THROUGH - 1] + 1)
        born = max(0, int(np.ceil(z["members"][0]["known_pos"])))
        dead = int(min(n, np.floor(dead))) if np.isfinite(dead) else n
        if born >= n or dead <= born:
            continue
        add.setdefault(born, []).append(z)
        rem.setdefault(dead, []).append(z)
        for r in rs:
            if r["type"] in ("rejection", "sweep", "retest_hold"):
                hold.setdefault(r["resolved_idx"], []).append(z["id"])
        for m in z["members"]:
            join.setdefault(max(0, int(np.ceil(m["known_pos"]))), []).append((z["id"], m["interval"]))
    out = np.full((n, 6), np.nan)
    prices: list[float] = []
    ids: list[int] = []
    holds: dict[int, int] = {}
    tfs: dict[int, set] = {}
    near = {"sup": np.full(n, np.nan), "res": np.full(n, np.nan)}
    atr = base.atr
    for t in range(n):
        for z in rem.get(t, ()):
            i = bisect.bisect_left(prices, z["price"])
            while i < len(ids) and ids[i] != z["id"]:
                i += 1
            if i < len(ids):
                prices.pop(i)
                ids.pop(i)
        for z in add.get(t, ()):
            i = bisect.bisect_left(prices, z["price"])
            prices.insert(i, z["price"])
            ids.insert(i, z["id"])
        for zid, iv in join.get(t, ()):
            tfs.setdefault(zid, set()).add(iv)
        for zid in hold.get(t, ()):
            holds[zid] = holds.get(zid, 0) + 1
        a = atr[t]
        if not prices or not np.isfinite(a) or a <= 0:
            continue
        i = bisect.bisect_right(prices, c[t])
        if i > 0:
            zid = ids[i - 1]
            out[t, 0:3] = ((c[t] - prices[i - 1]) / a, holds.get(zid, 0), len(tfs.get(zid, ())))
            near["sup"][t] = prices[i - 1]
        if i < len(prices):
            zid = ids[i]
            out[t, 3:6] = ((prices[i] - c[t]) / a, holds.get(zid, 0), len(tfs.get(zid, ())))
            near["res"][t] = prices[i]
    return out, near


# ------------------------------------------------------------------ labels

def labels(h: np.ndarray, l: np.ndarray, c: np.ndarray, pip: float, sl_pips: float, tp_pips: float,
           tp2_pips: float, horizon: int, spread_pips: float) -> dict[str, np.ndarray]:
    """Entry at each candle's close; both directions simulated on the next `horizon` candles.
    Stop and target inside the same candle = the stop. NaN where the horizon is not complete."""
    n = len(c)
    H = int(horizon)
    nan = np.full(n, np.nan)
    out = {k: nan.copy() for k in ("long_tp1", "short_tp1", "long_tp2", "short_tp2", "long_r", "short_r")}
    m = n - H
    if m <= 0:
        out["best"] = np.array([None] * n, dtype=object)
        return out
    hw = np.lib.stride_tricks.sliding_window_view(h[1:], H)[:m]
    lw = np.lib.stride_tricks.sliding_window_view(l[1:], H)[:m]
    e = c[:m, None]
    sl, big = sl_pips * pip, H + 1

    def first(mask: np.ndarray) -> np.ndarray:
        return np.where(mask.any(axis=1), mask.argmax(axis=1), big)
    stop_l, stop_s = first(lw <= e - sl), first(hw >= e + sl)
    end = c[H:H + m]
    for name, tp in (("tp1", tp_pips), ("tp2", tp2_pips)):
        d = tp * pip
        win_l = first(hw >= e + d) < stop_l  # same candle -> the stop counts
        win_s = first(lw <= e - d) < stop_s
        out[f"long_{name}"][:m], out[f"short_{name}"][:m] = win_l, win_s
        if name == "tp1":
            cost = spread_pips / sl_pips
            out["long_r"][:m] = np.where(win_l, tp / sl_pips, np.where(stop_l < big, -1.0,
                                                                       (end - c[:m]) / sl)) - cost
            out["short_r"][:m] = np.where(win_s, tp / sl_pips, np.where(stop_s < big, -1.0,
                                                                        (c[:m] - end) / sl)) - cost
    lt, st_ = out["long_tp1"], out["short_tp1"]
    if sl_pips < tp_pips:
        assert not np.any((lt[:m] == 1) & (st_[:m] == 1)), "long and short tp1 cannot both win when sl < tp"
    best = np.array([None] * n, dtype=object)
    best[:m] = "hold"
    best[:m][st_[:m] == 1] = "sell"
    both = (lt[:m] == 1) & (st_[:m] == 1)
    best[:m][(lt[:m] == 1) & ~both] = "buy"
    best[:m][both & (out["long_r"][:m] >= out["short_r"][:m])] = "buy"
    out["best"] = best
    return out


# ------------------------------------------------------------------ matrix

def pattern_labels(states: dict, poss: dict, ctxs: dict, roles: dict, blocks: dict, h, l, c, atr, pip: float,
                   tp_pips: float, horizon: int) -> dict[str, np.ndarray]:
    """Binary outcome labels for specific situations (NaN where the row is not that situation or its outcome
    is not known yet). These look AHEAD by design; they are labels, never features."""
    n = len(c)
    H = int(horizon)
    out = {k: np.full(n, np.nan) for k in PATTERN_LABELS}
    app = {k: np.zeros(n, bool) for k in PATTERN_LABELS}  # is the row that situation (known at the row itself)
    win = np.lib.stride_tricks.sliding_window_view
    big = H + 1

    def first(mask):
        return np.where(mask.any(axis=1), mask.argmax(axis=1), big)

    def cont(closes_arr, hi, lo):
        """Per index: (up wins, down wins) = close beyond `hi` before a close beyond `lo` within H (and mirror)."""
        m = len(closes_arr) - H
        up, dn = np.full(len(closes_arr), np.nan), np.full(len(closes_arr), np.nan)
        if m > 0:
            w = win(closes_arr[1:], H)[:m]
            with np.errstate(invalid="ignore"):
                fa, fb = first(w > hi[:m, None]), first(w < lo[:m, None])
            up[:m], dn[:m] = fa < fb, fb < fa
        return up, dn
    # 1. continuation after a pullback on the trigger timeframe
    names = [x for x, _ in TF_FEATURES]
    tr = blocks["tr"]
    pdir = tr[:, names.index("pullback_dir")]
    st = states["tr"]
    up, dn = cont(c, st["h1"], st["l1"])
    out["cont_after_pullback"] = np.where(pdir > 0, up, np.where(pdir < 0, dn, np.nan))
    app["cont_after_pullback"] = np.nan_to_num(pdir) != 0
    # 2. the same on the first higher timeframe
    if "ht1" in states:
        s1, p1 = states["ht1"], poss["ht1"]
        hc = ctxs[roles["ht1"]].df["c"].to_numpy(float)
        up1, dn1 = cont(hc, s1["h1"], s1["l1"])
        ok = p1 >= 0
        pp = np.where(ok, p1, 0)
        pd1 = blocks["ht1"][:, names.index("pullback_dir")]
        out["ht1_cont_after_pullback"] = np.where(ok & (pd1 > 0), up1[pp], np.where(ok & (pd1 < 0), dn1[pp], np.nan))
        app["ht1_cont_after_pullback"] = ok & (np.nan_to_num(pd1) != 0)
    # 3 + 4. fakeout / retest-then-continue on trigger-timeframe box breaks
    tp = tp_pips * pip
    for b in st["breaks"]:
        i0, side, edge, back = b["idx"], b["side"], b["edge"], b["back_idx"]
        app["fakeout"][i0] = True
        app["retest_then_continue"][i0:min(i0 + 3, (back - 1) if back is not None else n - 1, n - 1) + 1] = True
        if back is not None and back - i0 <= M.FAKEOUT_MAX:
            out["fakeout"][i0] = 1.0
        elif i0 + M.FAKEOUT_MAX < n:
            out["fakeout"][i0] = 0.0
        last_row = min(i0 + 3, (back - 1) if back is not None else n - 1, n - 1)
        for i in range(i0, last_row + 1):
            if i + H >= n:
                break
            tol = M.BREAK_TOL_ATR * atr[i] if np.isfinite(atr[i]) else 0.0
            touched, res = False, 0.0
            for j in range(i + 1, i + H + 1):
                if side * (c[j] - edge) <= 0:  # body closed back inside
                    break
                if touched and side * ((h[j] if side > 0 else l[j]) - edge) >= tp:
                    res = 1.0
                    break
                if not touched and side * ((l[j] if side > 0 else h[j]) - edge) <= tol:
                    touched = True
            out["retest_then_continue"][i] = res
    return out, app


def build_matrix(ctxs: dict[str, Context], interval: str, pip: float, sl_pips: float = 20, tp_pips: float = 50,
                 tp2_pips: float = 100, horizon: int = 48, spread_pips: float = 0.0, h4_off: int = 1,
                 with_labels: bool = True, sym: Optional[Symbol] = None, currencies: tuple = (),
                 strength: Optional[dict] = None, own: Optional[dict] = None) -> dict:
    """Pure: contexts of CLOSED candles (trigger + higher timeframes, optionally '1d' / '1wk' for the context
    blocks) -> feature matrix + labels for every trigger candle. Missing timeframes give null features.
    `currencies` selects the news events; `strength` / `own` are the currency-strength tables (None = null)."""
    hi = HIGHER[interval]
    roles = {"tr": interval, "ht1": hi[0], "ht2": hi[1]}
    base = ctxs[interval]
    df = base.df
    n = len(df)
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    closes = ns_index(base.closes)
    atr = base.atr.copy()
    atr[~(np.isfinite(atr) & (atr > 0))] = np.nan
    states, blocks, forming_raw, poss = {}, [], {}, {}
    by_iv: dict[str, tuple] = {}
    regimes = []
    tfb: dict[str, np.ndarray] = {}

    def state_of(iv: str):
        if iv not in by_iv:
            ctx = ctxs[iv]
            pos = np.arange(n) if iv == interval else ns_index(ctx.closes).searchsorted(closes, side="right") - 1
            stt = tf_state(ctx)
            by_iv[iv] = (stt, np.asarray(pos), tf_block(stt, np.asarray(pos), c, atr, iv == interval))
        return by_iv[iv]
    for role in ROLES:
        iv = roles[role]
        if iv not in ctxs:
            blocks.append(np.full((n, len(TF_FEATURES)), np.nan))
            regimes.append(np.full(n, np.nan))
            continue
        stt, pos, blk = state_of(iv)
        states[role], poss[role], tfb[role] = stt, pos, blk
        blocks.append(blk)
        regimes.append(np.where(pos >= 0, stt["regime"][np.maximum(pos, 0)], np.nan))
    fblocks = []
    for role in ROLES[1:]:
        fb, raw = forming_block(df, closes, atr, roles[role], h4_off)
        fblocks.append(fb)
        forming_raw[role] = raw
    tblock, sess = time_block(closes)
    atr_s = pd.Series(atr, index=closes)
    med = atr_s.rolling("20D", closed="left").median().to_numpy()
    rng = h - l
    with np.errstate(invalid="ignore", divide="ignore"):
        safe = np.where(rng > 0, rng, np.nan)
        vol = np.column_stack([atr / pip, atr / med, sl_pips * pip / atr, tp_pips * pip / atr, tp2_pips * pip / atr])
        cand = np.column_stack([(c - o) / safe, (h - np.maximum(o, c)) / safe, (np.minimum(o, c) - l) / safe,
                                (c - l) / safe, rng / atr]
                               + [(c - np.concatenate([np.full(k, np.nan), c[:-k]])) / atr if n > k
                                  else np.full(n, np.nan) for k in (4, 16, 96)])
    zblock, near = zone_block(ctxs, roles, pip)
    reg = np.column_stack(regimes)
    align = np.column_stack([np.nansum((reg == 1) | (reg == 3), axis=1), np.nansum((reg == 2) | (reg == 4), axis=1)])
    # compact daily / weekly context
    tf_names = [x for x, _ in TF_FEATURES]
    cols = [tf_names.index(x) for x in CTX_FEATURES]
    cblocks = []
    for role, iv in CTX_ROLES.items():
        cblocks.append(state_of(iv)[2][:, cols] if iv in ctxs else np.full((n, len(CTX_FEATURES)), np.nan))
    # candlestick + chart patterns on the trigger and the first higher timeframe
    pblocks = []
    for role in ("tr", "ht1"):
        iv = roles[role]
        if iv not in ctxs:
            pblocks.append(np.full((n, len(PT.CS_FEATURES) + len(PT.FAMILIES) * len(PT.CP_FIELDS)), np.nan))
            continue
        cx = ctxs[iv]
        stt, pos, _ = state_of(iv)
        cdf = cx.df
        co, ch, cl, cc = (cdf[x].to_numpy(float) for x in ("o", "h", "l", "c"))
        if role == "tr":
            sup, res_ = zblock[:, 0], zblock[:, 3]
        else:
            with np.errstate(invalid="ignore", divide="ignore"):
                sup, res_ = (cc - stt["l1"]) / cx.atr, (stt["h1"] - cc) / cx.atr
        cs = PT.candle_patterns(co, ch, cl, cc, cx.atr_prev, sup, res_)
        ok_ = pos >= 0
        cs_rows = np.where(ok_[:, None], cs[np.where(ok_, pos, 0)], np.nan)
        pblocks.append(np.column_stack([cs_rows, PT.chart_block(PT.chart_pattern_state(cx), pos, c, atr)]))
    nblock = EV.news_block(closes, tuple(currencies))
    sblock = ST.strength_block(sym, closes, strength, own) if strength is not None else \
        np.full((n, len(ST.STRENGTH_FEATURES)), np.nan)
    X = np.column_stack([b.astype(np.float32) for b in blocks + fblocks + [tblock, vol, cand, zblock, align]
                         + cblocks + [nblock, sblock] + pblocks])
    X[~np.isfinite(X)] = np.nan
    names = feature_names()
    assert X.shape[1] == len(names), (X.shape, len(names))
    y = labels(h, l, c, pip, sl_pips, tp_pips, tp2_pips, horizon, spread_pips) if with_labels else None
    pl, applicable = pattern_labels(states, poss, ctxs, roles, tfb, h, l, c, atr, pip, tp_pips, horizon)
    return {"names": names, "X": X, "y": y, "pattern_labels": pl, "applicable": applicable, "closes": closes,
            "close": c, "roles": roles,
            "states": states, "pos": poss,
            "forming": forming_raw, "near": near, "sess": sess, "atr": atr, "n": n}


# ------------------------------------------------------------------ service
#
# Long histories are computed in fixed chunks of the trigger series: chunk k covers rows [k*CHUNK, (k+1)*CHUNK)
# and is computed from row k*CHUNK - CHUNK_WARM onward (the grid is anchored at the first candle of the series,
# so it does not move when the series is cut for a replay or grows at the end). /features computes only the
# last chunk, which makes it the same numbers as the /dataset row and fast on years of history.

CHUNK, CHUNK_WARM, HTF_WARM = 20_000, 6_000, 300
MAX_1M_ROWS = 500_000
EXPORT_DIR = Path(__file__).resolve().parent.parent / ".cache" / "exports"
BEST_CODE = {"hold": 0, "buy": 1, "sell": 2}
BASE_LABEL_DOCS = {
    "long_tp1": "1 if +tp_pips is reached before -sl_pips within `horizon` candles (stop first in the same candle)",
    "short_tp1": "the same for a short", "long_tp2": "the same with tp2_pips", "short_tp2": "short with tp2_pips",
    "long_r": "realised R of the long tp1 trade net of spread (timeout marked to the last close)",
    "short_r": "the same for the short", "best": "buy if long_tp1, sell if short_tp1, else hold",
}


def _clean_matrix(X: np.ndarray) -> list:
    X = np.asarray(X, dtype=np.float64)
    obj = np.round(X, 5).astype(object)
    obj[np.isnan(X)] = None
    return obj.tolist()


def _clean_vec(v: np.ndarray, as_int: bool = False) -> list:
    if v.dtype == object:
        return v.tolist()
    nanm = np.isnan(v)
    obj = np.where(nanm, 0, v).astype(int).astype(object) if as_int else np.round(v, 5).astype(object)
    obj[nanm] = None
    return obj.tolist()


def _resolve(sym: Symbol, interval: str, pip, spread_pips):
    if interval not in HIGHER:
        raise ValueError(f"interval must be one of {list(HIGHER)} (trigger timeframe)")
    pip_used = float(pip) if pip else default_pip(sym)
    spread = float(spread_pips) if spread_pips is not None else default_spread_price(sym) / pip_used
    return pip_used, spread


def news_coverage(sym: Symbol) -> dict:
    """Event types available per currency of the symbol (empty list = that currency is not covered)."""
    have: dict[str, set] = {}
    for e in EV.load_table().get("events", []):
        if e["scheduled"]:
            have.setdefault(e["currency"], set()).add(e["type"])
    return {c_: sorted(have.get(c_, ())) for c_ in news_currencies(sym)}


def news_currencies(sym: Symbol) -> tuple:
    if sym.asset_class == "metal":
        return ("USD",)
    return tuple(sym.currencies) or ("USD",)


def _frames(sym: Symbol, interval: str) -> dict:
    """Closed candles of the trigger, its higher timeframes and the daily / weekly context (no structure yet)."""
    from . import m1store
    from .data import close_times, day_close_mode, get_series, resample_ohlc
    from .structure import closed_frame

    def load():
        now = now_ts()
        mode = day_close_mode(sym)
        frames, missing = {}, []
        for iv in dict.fromkeys((interval, *HIGHER[interval], "1d")):
            if iv == "1wk":
                continue
            try:
                ser = get_series(sym, iv)
                df, closes, _, _ = closed_frame(ser.df, iv, mode, now)
                if len(df) < (30 if iv == interval else 10):
                    raise ValueError(f"only {len(df)} closed candles")
                frames[iv] = (df, ns_index(closes), ser)
            except Exception as e:
                if iv == interval:
                    raise
                missing.append(f"{iv}: {type(e).__name__}: {str(e)[:120]}")
        if "1d" in frames:  # weekly candles are built from the daily ones (same feed, same levels)
            ddf, _, dser = frames["1d"]
            wk = resample_ohlc(ddf, "W-MON")
            wcl = close_times(wk.index, "1wk", mode)
            keep = np.asarray(wcl <= now)
            if keep.sum() >= 10:
                frames["1wk"] = (wk[keep], ns_index(wcl[keep]), dser)
            else:
                missing.append(f"1wk: only {int(keep.sum())} closed weekly candles")
        df, closes, ser = frames[interval]
        return {"frames": frames, "missing": missing, "info": ser.source_info or {"source": ser.source},
                "n": len(df), "fingerprint": (len(df), iso(closes[-1]), iso(df.index[0]),
                                              hash(m1store.fingerprint(sym.id)), EV.load_table().get("built"))}
    return cache.get_or_set(("ds_frames", sym.id, interval, replay_key()), TTL_INTRADAY, load)


def _chunk(sym: Symbol, fr: dict, interval: str, k: int, P: tuple) -> dict:
    """Feature matrix of chunk k (rows lo..hi of the trigger series, lo includes the warm-up). Chunks other than
    the last are computed LABEL_MARGIN candles past their end so that pattern labels can see their outcome
    (features are causal, so the extra candles do not change them)."""
    from .structure import context_from_frame
    pip, sl, tp, tp2, horizon, spread = P
    df, closes, ser = fr["frames"][interval]
    n = len(df)
    lo, hi = max(0, k * CHUNK - CHUNK_WARM), min(n, (k + 1) * CHUNK)
    hi_calc = min(n, hi + LABEL_MARGIN)
    ctxs = {interval: context_from_frame(sym, interval, df.iloc[lo:hi_calc], closes[lo:hi_calc], None, ser.source,
                                         ser.delayed_minutes, source_info=ser.source_info)}
    for iv in dict.fromkeys((*HIGHER[interval], *CTX_ROLES.values())):
        if iv not in fr["frames"] or iv == interval:
            continue
        hdf, hcl, hser = fr["frames"][iv]
        i0 = max(0, int(hdf.index.searchsorted(df.index[lo])) - HTF_WARM)
        i1 = int(hcl.searchsorted(closes[hi_calc - 1], side="right"))  # higher-timeframe candles closed by then
        if i1 - i0 >= 10:
            ctxs[iv] = context_from_frame(sym, iv, hdf.iloc[i0:i1], hcl[i0:i1], None, hser.source,
                                          hser.delayed_minutes, source_info=hser.source_info)
    use_strength = ST.applies(sym)
    res = build_matrix(ctxs, interval, pip, sl, tp, tp2, horizon, spread, h4_offset(sym), with_labels=False,
                       sym=sym, currencies=news_currencies(sym),
                       strength=ST.strength_table() if use_strength else None,
                       own=ST.own_z(sym) if use_strength and sym.asset_class == "metal" else None)
    res.update(lo=lo, hi=hi, ctxs=ctxs)
    return res


def _last_chunk(sym: Symbol, fr: dict, interval: str, P: tuple) -> dict:
    k = (fr["n"] - 1) // CHUNK
    key = ("ds_last", sym.id, interval, P, k, fr["fingerprint"], replay_key())
    return cache.get_or_set(key, TTL_INTRADAY, lambda: _chunk(sym, fr, interval, k, P))


def _rows(sym: Symbol, fr: dict, interval: str, P: tuple, a: int, b: int) -> tuple[np.ndarray, dict]:
    """Feature rows [a, b) of the trigger series and their pattern labels, assembled chunk by chunk."""
    n = fr["n"]
    out = np.full((max(b - a, 0), len(feature_names())), np.nan, dtype=np.float32)
    pl = {k: np.full(max(b - a, 0), np.nan, dtype=np.float32) for k in PATTERN_LABELS}
    if b <= a:
        return out, pl
    last_k = (n - 1) // CHUNK
    for k in range(a // CHUNK, (b - 1) // CHUNK + 1):
        res = _last_chunk(sym, fr, interval, P) if k == last_k else _chunk(sym, fr, interval, k, P)
        g0, g1 = max(a, k * CHUNK), min(b, (k + 1) * CHUNK)
        out[g0 - a:g1 - a] = res["X"][g0 - res["lo"]:g1 - res["lo"]]
        for name in pl:
            pl[name][g0 - a:g1 - a] = res["pattern_labels"][name][g0 - res["lo"]:g1 - res["lo"]]
    return out, pl


def _labels(sym: Symbol, fr: dict, interval: str, P: tuple) -> dict:
    pip, sl, tp, tp2, horizon, spread = P
    df = fr["frames"][interval][0]
    key = ("ds_labels", sym.id, interval, P, fr["fingerprint"], replay_key())
    return cache.get_or_set(key, TTL_INTRADAY, lambda: labels(
        df["h"].to_numpy(float), df["l"].to_numpy(float), df["c"].to_numpy(float), pip, sl, tp, tp2, horizon, spread))


def _range(fr: dict, interval: str, start, end, max_rows: Optional[int]) -> tuple[int, int]:
    from .data import parse_as_of
    closes = fr["frames"][interval][1]
    n = fr["n"]
    a = int(closes.searchsorted(parse_as_of(start, True), side="left")) if start else 0
    b = int(closes.searchsorted(parse_as_of(end, True), side="right")) if end else n
    a = max(a, min(WARMUP, n))
    if max_rows is not None:
        a = max(a, b - int(max_rows))
    elif interval == "1min" and not start:
        a = max(a, b - MAX_1M_ROWS)  # one-minute exports are capped unless a start is given
    return a, max(a, b)


def _notes(sym: Symbol, fr: dict, interval: str, pip: float, sl: float, tp: float, spread: float) -> list[str]:
    hist = "; ".join(f"{iv}: {len(df)} closed candles {iso(df.index[0])[:10]} → {iso(cl[-1])[:16]}Z "
                     f"({str(ser.source).split(';')[0][:70]})" for iv, (df, cl, ser) in fr["frames"].items())
    be = (sl + spread) / (sl + tp)
    hi = HIGHER[interval]
    ev = EV.load_table()
    cur = news_currencies(sym)
    notes = [
        f"History used — {hist}. Rows start after a {WARMUP}-candle warm-up of the trigger timeframe.",
        f"Roles: tr = {PUBLIC_IV.get(interval, interval)}, ht1 = {hi[0]}, ht2 = {hi[1]}, d1 = 1d, w1 = 1wk (weekly "
        f"candles are built from the daily ones). Distances are in ATR14 of the trigger timeframe; ages / counts of "
        f"the other timeframes are in candles of that timeframe, as of its last CLOSED candle.",
        f"News: scheduled high-impact events from the official schedule table ({len(ev.get('events', []))} rows, built "
        f"{ev.get('built')}); unscheduled meetings are never used. Coverage for this symbol — "
        + "; ".join(f"{c_}: {', '.join(t_) if t_ else 'MISSING (no events for this currency)'}"
                    for c_, t_ in news_coverage(sym).items())
        + ". The table only has USD, EUR and JPY events: the Bank of England site refused our client, so GBP "
          "central_bank coverage is MISSING, and AUD/CAD/CHF/NZD have no events either. News features describe only "
          "the covered currencies and are null when neither currency is covered. Released values (actual vs "
          "forecast) are not included.",
        "Currency strength: from 5m candles of the seven USD majors (NaN where fewer than 5 are available); for gold "
        "the base value is gold's own volatility-normalised move and the quote value is USD strength.",
        f"{sym.id} pip = {pip:g}: stop {sl:g} pips = {sl * pip:g}, target {tp:g} pips = {tp * pip:g} in price. Spread "
        f"{spread:g} pips is deducted from long_r / short_r only (it does not move the stop/target touch test). "
        f"A driftless random walk wins tp1 about sl/(sl+tp) = {sl / (sl + tp):.3f} of the time; breakeven win rate "
        f"with the spread is {be:.3f}.",
        f"Long histories are computed in chunks of {CHUNK} trigger candles, each warmed up on the {CHUNK_WARM} candles "
        f"before it ({HTF_WARM} candles for the higher timeframes), so structure state never depends on data older "
        f"than that. /features uses the same chunk, so it equals the /dataset row.",
        "Approximations: higher-timeframe structure only updates when that timeframe's candle closes; the forming "
        "higher-timeframe candle is rebuilt from trigger candles (the daily one uses the 17:00 New York day for every "
        "symbol); zone touch counts start at the beginning of the chunk warm-up; regime is the causal version of "
        "the /mtf regime (same rules, swings as confirmed at that time).",
        "A higher-timeframe candle counts from its nominal close time; on the 18:00 New York metals grid the last H4 "
        "candle of a session nominally closes an hour after trading stops, so it is first used by the next session's "
        "rows.",
        "Labels use candle highs/lows of this feed (bid); a stop and a target inside one candle count as the "
        "stop; rows whose horizon is not complete have null labels.",
    ]
    if fr["missing"]:
        notes.append("Missing higher timeframes (their features are null): " + "; ".join(fr["missing"]))
    st = (fr["info"].get("history") or {}).get("stitch")
    if st:
        notes.append("Local M1 store + live feed: " + str(st.get("note")))
    return notes


def _meta(sym: Symbol, fr: dict, interval: str, P: tuple, a: int, b: int, lab: dict) -> dict:
    pip, sl, tp, tp2, horizon, spread = P
    closes = fr["frames"][interval][1]
    hi = HIGHER[interval]
    return {
        "symbol": sym.id, "interval": PUBLIC_IV.get(interval, interval), "pip": pip,
        "params": {"sl_pips": sl, "tp_pips": tp, "tp2_pips": tp2, "horizon": horizon, "spread_pips": spread},
        "feature_version": FEATURE_VERSION, "feature_names": feature_names(),
        "feature_groups": feature_groups(),
        "news_coverage": news_coverage(sym),
        "label_names": list(PATTERN_LABELS),
        "label_docs": {**BASE_LABEL_DOCS, **PATTERN_LABELS},
        "timeframes": {"tr": PUBLIC_IV.get(interval, interval), "ht1": hi[0], "ht2": hi[1], "d1": "1d", "w1": "1wk"},
        "source": fr["info"].get("source"), "n": int(b - a),
        "labelled": int(np.sum(~np.isnan(lab["long_tp1"][a:b]))),
        "start": iso(closes[a]) if b > a else None, "end": iso(closes[b - 1]) if b > a else None,
        "notes": _notes(sym, fr, interval, pip, sl, tp, spread),
    }


def dataset(sym: Symbol, interval: str = "15m", pip: Optional[float] = None, sl_pips: float = 20,
            tp_pips: float = 50, tp2_pips: float = 100, horizon: int = 48, spread_pips: Optional[float] = None,
            max_rows: int = 20000, start: Optional[str] = None, end: Optional[str] = None) -> dict:
    """JSON export (capped by max_rows: the LAST max_rows rows of the requested range)."""
    pip_used, spread = _resolve(sym, interval, pip, spread_pips)
    P = (pip_used, sl_pips, tp_pips, tp2_pips, horizon, spread)
    fr = _frames(sym, interval)
    a, b = _range(fr, interval, start, end, max_rows)
    X, pl = _rows(sym, fr, interval, P, a, b)
    y = _labels(sym, fr, interval, P)
    closes = fr["frames"][interval][1]
    times = [iso(t) for t in closes[a:b]]
    meta = _meta(sym, fr, interval, P, a, b, y)
    sl_ = slice(a, b)
    return {
        **{k: meta[k] for k in ("symbol", "interval", "pip", "params", "feature_version", "feature_names",
                                "feature_groups", "label_names", "label_docs", "timeframes")},
        "pattern_labels": {k: _clean_vec(v.astype(float), True) for k, v in pl.items()},
        "times": times, "X": _clean_matrix(X),
        "y": {"long_tp1": _clean_vec(y["long_tp1"][sl_], True), "short_tp1": _clean_vec(y["short_tp1"][sl_], True),
              "long_tp2": _clean_vec(y["long_tp2"][sl_], True), "short_tp2": _clean_vec(y["short_tp2"][sl_], True),
              "long_r": _clean_vec(y["long_r"][sl_]), "short_r": _clean_vec(y["short_r"][sl_]),
              "best": y["best"][sl_].tolist()},
        "n": meta["n"], "start": meta["start"], "end": meta["end"], "labelled": meta["labelled"],
        "source": meta["source"], "as_of": meta["end"], "notes": meta["notes"],
    }


def dataset_npz(sym: Symbol, interval: str = "15m", pip: Optional[float] = None, sl_pips: float = 20,
                tp_pips: float = 50, tp2_pips: float = 100, horizon: int = 48,
                spread_pips: Optional[float] = None, start: Optional[str] = None, end: Optional[str] = None,
                max_rows: Optional[int] = None) -> Path:
    """Binary export (no row cap): numpy .npz with X float32, times int64 epoch seconds, label arrays float32,
    best int8 (0 hold, 1 buy, 2 sell, -1 unlabelled) and meta (JSON string). Finished files are cached on disk,
    keyed by symbol, interval, parameters and the data fingerprint."""
    import hashlib
    import json
    import time as _time
    pip_used, spread = _resolve(sym, interval, pip, spread_pips)
    P = (pip_used, sl_pips, tp_pips, tp2_pips, horizon, spread)
    fr = _frames(sym, interval)
    a, b = _range(fr, interval, start, end, max_rows)
    key = json.dumps([sym.id, interval, P, a, b, FEATURE_VERSION, fr["fingerprint"], CHUNK, CHUNK_WARM, LABEL_MARGIN],
                     default=str)
    path = EXPORT_DIR / f"{sym.id}_{PUBLIC_IV.get(interval, interval)}_{hashlib.sha1(key.encode()).hexdigest()[:16]}.npz"
    if path.exists():
        return path
    with _export_lock(path.name):  # two callers asking for the same export: the second waits and reuses the file
        return _write_export(path, sym, fr, interval, P, a, b)


_export_locks: dict[str, threading.Lock] = {}
_export_guard = threading.Lock()


def _export_lock(name: str) -> threading.Lock:
    with _export_guard:
        return _export_locks.setdefault(name, threading.Lock())


def _write_export(path: Path, sym: Symbol, fr: dict, interval: str, P: tuple, a: int, b: int) -> Path:
    import json
    import os
    import time as _time
    if path.exists():
        return path
    t0 = _time.time()
    X, pl = _rows(sym, fr, interval, P, a, b)
    y = _labels(sym, fr, interval, P)
    closes = fr["frames"][interval][1]
    meta = _meta(sym, fr, interval, P, a, b, y)
    meta["build_seconds"] = round(_time.time() - t0, 1)
    best = np.full(b - a, -1, dtype=np.int8)
    bl = y["best"][a:b]
    for name, code in BEST_CODE.items():
        best[bl == name] = code
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.{threading.get_ident()}.tmp.npz")
    np.savez_compressed(
        tmp, X=X, times=(closes[a:b].asi8 // 10 ** 9).astype(np.int64),
        **{k: y[k][a:b].astype(np.float32) for k in ("long_tp1", "short_tp1", "long_tp2", "short_tp2", "long_r",
                                                     "short_r")},
        **{k: v.astype(np.float32) for k, v in pl.items()},
        best=best, meta=np.array(json.dumps(meta)))
    tmp.replace(path)
    return path


_REG = ["consolidation", "impulse_up", "impulse_down", "trend_up", "trend_down"]


def features(sym: Symbol, interval: str = "15m", pip: Optional[float] = None, sl_pips: float = 20,
             tp_pips: float = 50, tp2_pips: float = 100, horizon: int = 48,
             spread_pips: Optional[float] = None) -> dict:
    from .mtf import make_data_note
    pip_used, spread = _resolve(sym, interval, pip, spread_pips)
    fr = _frames(sym, interval)
    res = dict(_last_chunk(sym, fr, interval, (pip_used, sl_pips, tp_pips, tp2_pips, horizon, spread)))
    res["info"] = fr["info"]
    t = res["n"] - 1  # last row of the chunk = last closed trigger candle
    x = res["X"][t]
    names = res["names"]
    v = dict(zip(names, x))
    price = float(res["close"][t])
    atr = float(res["atr"][t])

    def num(k):
        return None if np.isnan(v[k]) else float(v[k])
    tfacts = {}
    for role in ROLES:
        iv = res["roles"][role]
        st = res["states"].get(role)
        if st is None:
            tfacts[iv] = {"role": role, "available": False}
            continue
        cx = res["ctxs"][iv]
        j = int(res["pos"][role][t])
        if j < 0:
            tfacts[iv] = {"role": role, "available": False}
            continue
        reg = next((r for i, r in enumerate(_REG) if v[f"{role}_regime_{r}"] == 1), None)
        box = None
        if v[f"{role}_box_exists"] == 1:
            box = {"top": float(st["top"][j]), "bottom": float(st["bot"][j]),
                   "state": "inside" if v[f"{role}_box_inside"] == 1 else
                   ("closed_above" if st["status"][j] == M.OUT_UP else "closed_below"),
                   "position_0_1": num(f"{role}_box_pos"), "height_atr": num(f"{role}_box_height_atr"),
                   "age_candles": num(f"{role}_box_age"),
                   "sweeps_above": num(f"{role}_sweeps_above"), "sweeps_below": num(f"{role}_sweeps_below"),
                   "fakeouts_above": num(f"{role}_fakeouts_above"), "fakeouts_below": num(f"{role}_fakeouts_below")}
        tfacts[iv] = {
            "role": role, "regime": reg, "last_closed_candle_close": iso(cx.closes[j]),
            "swings": {"last_high": None if np.isnan(st["h1"][j]) else float(st["h1"][j]),
                       "last_high_label": {1.0: "HH", -1.0: "LH"}.get(v[f"{role}_last_high_label"], "="),
                       "last_low": None if np.isnan(st["l1"][j]) else float(st["l1"][j]),
                       "last_low_label": {1.0: "HL", -1.0: "LL"}.get(v[f"{role}_last_low_label"], "="),
                       "trend": {1.0: "up", -1.0: "down"}.get(v[f"{role}_swing_trend"], "mixed")},
            "pullback": {"direction": {1.0: "uptrend pullback", -1.0: "downtrend pullback"}.get(
                v[f"{role}_pullback_dir"], "none"), "depth_0_1": num(f"{role}_pullback_depth")},
            "box": box,
            "last_break": None if v[f"{role}_brk_dir"] == 0 or np.isnan(v[f"{role}_brk_dir"]) else {
                "direction": "up" if v[f"{role}_brk_dir"] > 0 else "down", "candles_ago": num(f"{role}_brk_age"),
                "edge": float(st["brk_edge"][j]), "distance_to_edge_atr": num(f"{role}_brk_edge_dist_atr")},
            "last_impulse": None if v[f"{role}_imp_dir"] == 0 or np.isnan(v[f"{role}_imp_dir"]) else {
                "direction": "up" if v[f"{role}_imp_dir"] > 0 else "down", "size_atr": num(f"{role}_imp_size_atr"),
                "candles_since_end": num(f"{role}_imp_age")},
        }
    forming = {}
    for role in ROLES[1:]:
        f = res["forming"][role]
        iv = res["roles"][role]
        forming[iv] = {"start": iso(f["start"][t]) if not pd.isna(f["start"][t]) else None,
                       "closes_at": iso(f["start"][t] + f["dur"]) if not pd.isna(f["start"][t]) else None,
                       "open": float(f["open"][t]), "high": float(f["high"][t]), "low": float(f["low"][t]),
                       "price": price, "direction": "up" if price > f["open"][t] else "down" if price < f["open"][t]
                       else "flat", "elapsed_0_1": num(f"{role}_form_elapsed"), "position_0_1": num(f"{role}_form_pos")}
    sess = [k for k in ("Sydney", "Tokyo", "London", "New York") if res["sess"][k][t]]
    facts = {
        "timeframes": tfacts, "forming": forming,
        "zones": {"support": None if np.isnan(res["near"]["sup"][t]) else {
            "level": float(res["near"]["sup"][t]), "distance_atr": num("sup_dist_atr"),
            "respected_touches": num("sup_touches"), "timeframes": num("sup_tfs")},
            "resistance": None if np.isnan(res["near"]["res"][t]) else {
                "level": float(res["near"]["res"][t]), "distance_atr": num("res_dist_atr"),
                "respected_touches": num("res_touches"), "timeframes": num("res_tfs")}},
        "session": {"active": sess or ["off-session"], "minutes_since_open": num("mins_since_session_open")},
        "volatility": {"atr": atr, "atr_pips": num("atr_pips"), "sl_atr": num("sl_atr"), "tp_atr": num("tp_atr")},
        "alignment": {"up": num("align_up"), "down": num("align_down")},
    }
    nxt = [e for e in EV.history(",".join(news_currencies(sym)), iso(res["closes"][t]), None, True)][:3]
    facts["news"] = {"minutes_to_next": num("news_mins_to_next"), "minutes_since_last": num("news_mins_since_last"),
                     "within_60m": bool(v["news_within_60m"] == 1), "today": bool(v["news_today"] == 1),
                     "next_scheduled": [{k: e[k] for k in ("time_utc", "currency", "type", "title")} for e in nxt]}
    facts["strength"] = {w: {"base": num(f"str_base_{w}"), "quote": num(f"str_quote_{w}"),
                             "diff": num(f"str_diff_{w}")} for w in ST.WINDOWS}
    facts["context"] = {CTX_ROLES[r]: {n_: num(f"{r}_{n_}") for n_ in CTX_FEATURES} for r in CTX_ROLES}
    pats = {}
    for role in ("tr", "ht1"):
        iv_ = PUBLIC_IV.get(res["roles"][role], res["roles"][role])
        cs_ = {n_[3:]: ("bullish" if v[f"{role}_{n_}"] > 0 else "bearish") if n_ != "cs_inside" else "yes"
               for n_, _ in PT.CS_FEATURES if not np.isnan(v[f"{role}_{n_}"]) and v[f"{role}_{n_}"] != 0}
        cp_ = {f_: {"direction": {1.0: "up", -1.0: "down"}.get(v[f"{role}_cp_{f_}_dir"], "undecided"),
                    "state": "confirmed" if v[f"{role}_cp_{f_}_state"] == 2 else "forming",
                    "age_candles": num(f"{role}_cp_{f_}_age"), "distance_to_trigger_atr": num(f"{role}_cp_{f_}_dist_atr")}
               for f_, _ in PT.FAMILIES if v[f"{role}_cp_{f_}_active"] == 1}
        pats[iv_] = {"candlestick": cs_, "chart": cp_}
    facts["patterns"] = pats
    return {
        "symbol": sym.id, "interval": PUBLIC_IV.get(interval, interval), "time": iso(res["closes"][t]),
        "price": price, "pip": pip_used,
        "params": {"sl_pips": sl_pips, "tp_pips": tp_pips, "tp2_pips": tp2_pips, "horizon": horizon,
                   "spread_pips": spread},
        "feature_version": FEATURE_VERSION, "feature_names": names, "feature_groups": feature_groups(),
        "feature_docs": feature_docs(),
        "applicable": {k: bool(m[t]) for k, m in res["applicable"].items()},
        "label_docs": dict(PATTERN_LABELS),
        "timeframes": {**{k: PUBLIC_IV.get(v_, v_) for k, v_ in res["roles"].items()}, "d1": "1d", "w1": "1wk"},
        "x": _clean_matrix(x.reshape(1, -1))[0], "facts": facts,
        "data_note": make_data_note(sym, res["info"]), "source": res["info"].get("source"),
        "as_of": iso(res["closes"][t]),
    }
