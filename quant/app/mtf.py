"""Top-down multi-timeframe read: consolidation vs impulse per timeframe, wick sweeps that failed to close
beyond, and a reasoned playbook (sell zone / buy zone when ranging, retest zone after a break).

House rules: candle BODIES / closes define structure; wicks are liquidity sweeps.

Box (per timeframe, causal): when the last confirmed body swings are not trending (not HH+HL, not LH+LL), the
box is  top = highest of the last two swing-high bodies,  bottom = lowest of the last two swing-low bodies
(a swing that is the origin of an impulse leg is left out, so a box starts where the impulse ended).
It stays the reference while closes remain inside (± 0.1 ATR tolerance). A body close beyond it puts the
timeframe in a `closed_above` / `closed_below` state (break -> retest) until price closes back inside
(failed break) or 24 candles pass. Everything about a candle uses only swings confirmed before it closed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import wilson_ci
from .cache import TTL_INTRADAY, TTL_MODEL, cache
from .data import INTERVALS, iso, now_ts, replay_key
from .levels import NULL_Z, shuffled_base
from .retest import default_pip
from .structure import Context, build_context, context_from_frame, session_label
from .symbols import Symbol

BREAK_TOL_ATR = 0.1  # a close must clear the edge by this much to count as a break
MAX_BROKEN = 24  # candles a broken box stays the reference
MIN_HEIGHT_ATR = 0.75
ZONE_ATR, ZONE_MAX_FRAC = 0.25, 0.3
IMPULSE_MIN_ATR, IMPULSE_SPEED_ATR, IMPULSE_FRESH = 2.5, 0.5, 8
SWEEP_MIN_ATR = 0.25
ODDS_HORIZON = 48
FAKEOUT_MAX = 3  # a body close outside that is back inside within this many candles is a fakeout
RETEST_WINDOW = 24
MIN_N = 30
NONE, INSIDE, OUT_UP, OUT_DOWN = 0, 1, 2, 3
_STATE = {NONE: "none", INSIDE: "inside", OUT_UP: "closed_above", OUT_DOWN: "closed_below"}


def _px(p: Optional[float]) -> str:
    if p is None or not np.isfinite(p):
        return "n/a"
    a = abs(p)
    return f"{p:.5f}" if a < 10 else f"{p:.3f}" if a < 1000 else f"{p:.2f}"


def _t(ts) -> str:
    txt = ts if isinstance(ts, str) else iso(ts)
    return txt[5:16].replace("T", " ") + "Z"


# ------------------------------------------------------------------ causal box state

@dataclass
class BoxState:
    top: np.ndarray  # active box top after each candle closed (nan = none)
    bot: np.ndarray
    start: np.ndarray  # index where the box window starts
    status: np.ndarray  # NONE / INSIDE / OUT_UP / OUT_DOWN after each candle
    break_idx: np.ndarray  # index of the candle that closed outside (-1)
    raw_valid: np.ndarray  # a swing box existed from the swings confirmed by this candle
    labels: list  # (last swing-high label, last swing-low label) after each candle
    episode: np.ndarray  # id of the box episode each candle belongs to (-1 = none)
    imp_dir: np.ndarray  # direction (+1/-1/0) of the last impulse leg between swings confirmed by this candle
    breaks: list  # every body close outside a box: dicts (idx, side, top, bot, back_idx, attempt, ...)
    episodes: list  # box lifetimes: dicts (id, first_idx, end_idx, top, bot, box_start, breaks, resolved)


def _alt_impulse(alt: list, atr: np.ndarray, upto_idx: Optional[int] = None) -> int:
    """Direction of the most recent impulse leg between the (as-of) alternating swings; with `upto_idx`, only
    legs that ended at or before that candle (the move that led INTO a box)."""
    for a, b in zip(reversed(alt[-9:-1]), reversed(alt[-8:])):
        if upto_idx is not None and b.idx > upto_idx:
            continue
        n = b.idx - a.idx
        av = atr[a.idx]
        if n > 0 and np.isfinite(av) and av > 0:
            mv = b.price - a.price
            if abs(mv) >= IMPULSE_MIN_ATR * av and abs(mv) / n >= IMPULSE_SPEED_ATR * av:
                return 1 if mv > 0 else -1
    return 0


def swing_box(alt: list, atr: np.ndarray, t: int) -> Optional[tuple]:
    """(top, bottom, start_idx) from the last alternating body swings, or None.
    Uses the last four swings (two highs + two lows); a swing that is the ORIGIN of an impulse leg is dropped,
    so the box starts at the impulse's end (three swings are then enough). No box while the swings trend
    (higher high + higher low, or lower high + lower low)."""
    S = alt[-4:]
    if len(S) >= 3:
        a, b = S[0], S[1]
        av = atr[a.idx]
        nb = b.idx - a.idx
        if nb > 0 and np.isfinite(av) and av > 0 and abs(b.price - a.price) >= IMPULSE_MIN_ATR * av \
                and abs(b.price - a.price) / nb >= IMPULSE_SPEED_ATR * av:
            S = S[1:]
    if len(S) < 3:
        return None
    hs = [x for x in S if x.kind == "H"]
    ls = [x for x in S if x.kind == "L"]
    if len(hs) == 2 and len(ls) == 2:
        hh, hl = hs[1].price > hs[0].price, ls[1].price > ls[0].price
        lh, ll = hs[1].price < hs[0].price, ls[1].price < ls[0].price
        if (hh and hl) or (lh and ll):
            return None
    tp, bt = max(x.price for x in hs), min(x.price for x in ls)
    if not np.isfinite(atr[t]) or tp - bt < MIN_HEIGHT_ATR * atr[t]:
        return None
    return tp, bt, min(x.idx for x in S)


def box_states(ctx: Context) -> BoxState:
    """One causal pass: swing box from confirmed swings + inside / closed-outside state machine.
    A box lifetime ("episode") starts when a box first forms and ends when a body close outside it has not
    been closed back inside for MAX_BROKEN candles. While closes stay inside, the box is redrawn from the
    newest swings; the FIRST box of the episode is kept as `outer_*` for reference."""
    df = ctx.df
    n = len(df)
    o = df["o"].to_numpy(float)
    c = df["c"].to_numpy(float)
    atr = ctx.atr
    hist = sorted(ctx.run.history, key=lambda s: (s.confirmed_idx, s.idx))
    hp = 0
    alt: list = []
    top = np.full(n, np.nan)
    bot = np.full(n, np.nan)
    start = np.full(n, -1)
    status = np.zeros(n, int)
    brk = np.full(n, -1)
    raw_valid = np.zeros(n, bool)
    episode = np.full(n, -1)
    imp_dir = np.zeros(n, int)
    labels: list = [None] * n
    breaks: list = []
    episodes: list = []
    a_top = a_bot = np.nan
    a_start, a_status, a_brk = -1, NONE, -1
    ep = None
    cur_break = None
    cand = None  # raw swing box known BEFORE the current candle closes
    lead = 0

    def new_break(t: int, side: int) -> dict:
        prev = [b for b in ep["breaks"] if b["side"] == side]
        ap = atr[t - 1] if t > 0 and np.isfinite(atr[t - 1]) else np.nan
        r = {"idx": t, "side": side, "top": a_top, "bot": a_bot, "edge": a_top if side > 0 else a_bot,
             "box_start": a_start, "back_idx": None, "attempt": len(prev) + 1, "episode": ep["id"],
             "imp_dir": ep["lead_impulse"], "close": float(c[t]), "open": float(o[t]),
             "body_atr": float(abs(c[t] - o[t]) / ap) if np.isfinite(ap) and ap > 0 else np.nan}
        breaks.append(r)
        ep["breaks"].append(r)
        return r

    for t in range(n):
        tol = BREAK_TOL_ATR * atr[t - 1] if t > 0 and np.isfinite(atr[t - 1]) else 0.0
        if a_status in (NONE, INSIDE) and cand is not None:
            a_top, a_bot, a_start = cand
            if a_status == NONE:
                ep = {"id": len(episodes), "first_idx": t, "end_idx": None, "breaks": [], "resolved": None,
                      "lead_impulse": lead, "outer_top": a_top, "outer_bot": a_bot, "first_box_start": a_start}
                episodes.append(ep)
            a_status = INSIDE
            ep.update(top=a_top, bot=a_bot, box_start=a_start)
        if a_status == INSIDE:
            if c[t] > a_top + tol:
                a_status, a_brk = OUT_UP, t
                cur_break = new_break(t, 1)
            elif c[t] < a_bot - tol:
                a_status, a_brk = OUT_DOWN, t
                cur_break = new_break(t, -1)
        elif a_status in (OUT_UP, OUT_DOWN):
            if a_bot - tol <= c[t] <= a_top + tol:
                cur_break["back_idx"] = t  # closed back inside: failed break, the range is alive again
                a_status, a_brk = INSIDE, -1
            elif a_status == OUT_UP and c[t] < a_bot - tol:
                cur_break["back_idx"] = t
                a_status, a_brk = OUT_DOWN, t
                cur_break = new_break(t, -1)
            elif a_status == OUT_DOWN and c[t] > a_top + tol:
                cur_break["back_idx"] = t
                a_status, a_brk = OUT_UP, t
                cur_break = new_break(t, 1)
            elif t - a_brk >= MAX_BROKEN:
                ep.update(end_idx=a_brk, resolved="up" if a_status == OUT_UP else "down")
                a_status, a_brk, a_top, a_bot, a_start, ep = NONE, -1, np.nan, np.nan, -1, None
        top[t], bot[t], start[t], status[t], brk[t] = a_top, a_bot, a_start, a_status, a_brk
        episode[t] = ep["id"] if ep is not None else -1
        # swings confirmed by this candle's close become usable from the next candle on
        changed = False
        while hp < len(hist) and hist[hp].confirmed_idx <= t:
            sw = hist[hp]
            hp += 1
            if alt and alt[-1].kind == sw.kind:
                alt.pop()
            alt.append(sw)
            changed = True
        cand = swing_box(alt, atr, t)
        if changed or cand is not None:
            lead = _alt_impulse(alt, atr, cand[2] if cand is not None else None)
        imp_dir[t] = lead
        raw_valid[t] = cand is not None
        if len(alt) >= 2:
            labels[t] = (next((x.label for x in reversed(alt) if x.kind == "H"), None),
                         next((x.label for x in reversed(alt) if x.kind == "L"), None))
    return BoxState(top, bot, start, status, brk, raw_valid, labels, episode, imp_dir, breaks, episodes)


def is_fakeout(b: dict, fakeout_max: int = FAKEOUT_MAX) -> bool:
    return b["back_idx"] is not None and b["back_idx"] - b["idx"] <= fakeout_max


def fakeout_followed(ctx: Context, b: dict, horizon: int = ODDS_HORIZON) -> dict:
    """After a fakeout closed back inside: did price reach mid / the opposite edge / break the opposite side?"""
    df = ctx.df
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    n = len(df)
    side, top, bot = b["side"], b["top"], b["bot"]
    mid, opp = (top + bot) / 2, (bot if side > 0 else top)
    back = l if side > 0 else h
    j0 = b["back_idx"]
    tol = BREAK_TOL_ATR * ctx.atr[j0] if np.isfinite(ctx.atr[j0]) else 0.0
    out = {"reached_mid": False, "reached_opposite_edge": False, "broke_opposite_side": False,
           "broke_same_side_again": False, "complete": j0 + horizon < n}
    for j in range(j0, min(n, j0 + horizon + 1)):
        if side * (back[j] - mid) <= 0:
            out["reached_mid"] = True
        if side * (back[j] - opp) <= 0:
            out["reached_opposite_edge"] = True
        if side * (c[j] - opp) < -tol:
            out["broke_opposite_side"] = True
            break
        if j > j0 and side * (c[j] - b["edge"]) > tol:
            out["broke_same_side_again"] = True
            break
    return out


def public_fakeout(ctx: Context, b: dict, pip: float) -> dict:
    f = fakeout_followed(ctx, b)
    what = ("broke the opposite side" if f["broke_opposite_side"] else
            "reached the opposite edge" if f["reached_opposite_edge"] else
            "broke the same side again" if f["broke_same_side_again"] else
            "reached mid" if f["reached_mid"] else "stayed near the edge")
    return {"time": iso(ctx.df.index[b["idx"]]), "close_time": iso(ctx.closes[b["idx"]]),
            "side": "above" if b["side"] > 0 else "below", "edge": b["edge"],
            "box": {"top": b["top"], "bottom": b["bot"]}, "close": b["close"],
            "close_beyond_pips": abs(b["close"] - b["edge"]) / pip,
            "candles_until_back_inside": int(b["back_idx"] - b["idx"]),
            "back_inside_time": iso(ctx.closes[b["back_idx"]]), "followed": f, "what_followed": what,
            "session": session_label(ctx.closes[b["idx"]])}


def box_history(ctx: Context, st: BoxState, pip: float, fakeout_max: int = FAKEOUT_MAX, limit: int = 4) -> list[dict]:
    """Completed boxes (resolved by a body close outside that was not closed back inside)."""
    df = ctx.df
    h, l = df["h"].to_numpy(float), df["l"].to_numpy(float)
    out = []
    for ep in [e for e in st.episodes if e["end_idx"] is not None][-limit:]:
        fo = [b for b in ep["breaks"] if is_fakeout(b, fakeout_max)]
        final = ep["breaks"][-1]
        before = [b for b in fo if b["idx"] < final["idx"]]
        opp_first = bool(before and before[-1]["side"] == -final["side"])
        s0 = max(ep["first_box_start"], 0)
        w = slice(s0, ep["end_idx"] + 1)
        out.append({
            "top": ep["top"], "bottom": ep["bot"], "height_pips": (ep["top"] - ep["bot"]) / pip,
            "first_top": ep["outer_top"], "first_bottom": ep["outer_bot"],
            "wick_high": float(h[w].max()), "wick_low": float(l[w].min()),
            "start": iso(df.index[s0]), "end": iso(ctx.closes[ep["end_idx"]]),
            "duration_candles": int(ep["end_idx"] - s0 + 1),
            "lead_in_impulse": {1: "up", -1: "down", 0: "none"}[ep["lead_impulse"]],
            "fakeouts_above": sum(b["side"] > 0 for b in fo), "fakeouts_below": sum(b["side"] < 0 for b in fo),
            "late_failed_breaks": sum(b["back_idx"] is not None and not is_fakeout(b, fakeout_max)
                                      for b in ep["breaks"]),
            "fakeouts": [public_fakeout(ctx, b, pip) for b in fo][-4:],
            "resolved": ep["resolved"], "final_break_close": final["close"],
            "final_break_time": iso(ctx.closes[final["idx"]]),
            "preceded_by_opposite_fakeout": opp_first,
        })
    return out


def prior_box_edges(ctx: Context, st: BoxState, last: float, pip: float, atr: float, max_atr: float = 6.0,
                    lookback: int = 12) -> list[dict]:
    """Edges of earlier boxes near the current price (they act as support / resistance)."""
    df = ctx.df
    out = []
    done = [e for e in st.episodes if e["end_idx"] is not None][-lookback:]
    for ep in done:
        lv = {"top": ep["top"], "bottom": ep["bot"]}
        if abs(ep["top"] - ep["outer_top"]) > 0.1 * atr:
            lv["first_top"] = ep["outer_top"]
        if abs(ep["bot"] - ep["outer_bot"]) > 0.1 * atr:
            lv["first_bottom"] = ep["outer_bot"]
        for kind, lvl in lv.items():
            if abs(lvl - last) <= max_atr * atr:
                out.append({"level": lvl, "edge": kind, "interval": ctx.interval,
                            "box_start": iso(df.index[max(ep["first_box_start"], 0)]),
                            "box_end": iso(ctx.closes[ep["end_idx"]]), "resolved": ep["resolved"],
                            "distance_pips": (lvl - last) / pip,
                            "role": "resistance" if lvl > last else "support"})
    return sorted(out, key=lambda x: abs(x["distance_pips"]))[:6]


def zone_width(height: float, atr: float) -> float:
    return float(min(ZONE_ATR * atr, ZONE_MAX_FRAC * height))


# ------------------------------------------------------------------ per-timeframe read

def legs(ctx: Context) -> list[dict]:
    """Moves between consecutive alternating body swings, plus the leg in progress."""
    df = ctx.df
    c = df["c"].to_numpy(float)
    sw = ctx.run.swings
    out = []
    pts = [(s.idx, s.price) for s in sw] + [(len(df) - 1, float(c[-1]))]
    for (i0, p0), (i1, p1) in zip(pts, pts[1:]):
        if i1 <= i0:
            continue
        atr = ctx.atr[i0] if np.isfinite(ctx.atr[i0]) else np.nan
        out.append({"start_idx": i0, "end_idx": i1, "from": p0, "to": p1, "move": p1 - p0, "candles": i1 - i0,
                    "atr": float(atr), "in_progress": i1 == len(df) - 1 and (not sw or sw[-1].idx != i1)})
    return out


def is_impulse(leg: dict) -> bool:
    a = leg["atr"]
    if not np.isfinite(a) or a <= 0:
        return False
    return abs(leg["move"]) >= IMPULSE_MIN_ATR * a and abs(leg["move"]) / leg["candles"] >= IMPULSE_SPEED_ATR * a


def tighten_start(ctx: Context, leg: dict) -> int:
    """Start the impulse at the LAST candle that was still near the leg's origin (drops a slow lead-in)."""
    o, c = ctx.df["o"].to_numpy(float), ctx.df["c"].to_numpy(float)
    up = leg["move"] > 0
    best = leg["start_idx"]
    for j in range(leg["start_idx"] + 1, leg["end_idx"]):
        ext = min(o[j], c[j]) if up else max(o[j], c[j])
        if abs(ext - leg["from"]) <= 0.5 * leg["atr"]:
            best = j
    return best


def last_impulse(ctx: Context, pip: float, upto_idx: Optional[int] = None) -> Optional[dict]:
    """Most recent impulse leg; with `upto_idx`, the most recent one that ended by that candle."""
    lg = legs(ctx)
    df = ctx.df
    for leg in reversed(lg[-14:]):
        if not is_impulse(leg) or (upto_idx is not None and leg["end_idx"] > upto_idx):
            continue
        s = tighten_start(ctx, leg)
        o, c = df["o"].to_numpy(float), df["c"].to_numpy(float)
        up = leg["move"] > 0
        frm = float(min(o[s], c[s]) if up else max(o[s], c[s])) if s != leg["start_idx"] else leg["from"]
        ncand = max(leg["end_idx"] - s, 1)
        return {"direction": "up" if up else "down", "from": frm, "to": leg["to"],
                "pips": abs(leg["to"] - frm) / pip, "atr_multiple": abs(leg["to"] - frm) / leg["atr"],
                "candles": int(ncand), "start": iso(df.index[s]), "end": iso(ctx.closes[leg["end_idx"]]),
                "candles_since_end": int(len(df) - 1 - leg["end_idx"]), "in_progress": leg["in_progress"]}
    return None


def box_sweeps(ctx: Context, top: float, bot: float, start: int, pip: float, limit: int = 6) -> list[dict]:
    """Wicks beyond the box edges that closed back inside, within the box window (largest first, then by time)."""
    df = ctx.df
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    out = []
    for t in range(max(start, 0), len(df)):
        a = ctx.atr[t - 1] if t > 0 and np.isfinite(ctx.atr[t - 1]) else ctx.atr[t]
        tol = BREAK_TOL_ATR * a
        if h[t] - top >= SWEEP_MIN_ATR * a and c[t] <= top + tol:
            out.append({"time": iso(df.index[t]), "close_time": iso(ctx.closes[t]), "side": "high", "level": top,
                        "wick_extreme": float(h[t]), "close": float(c[t]), "pips_beyond": (h[t] - top) / pip,
                        "session": session_label(ctx.closes[t])})
        if bot - l[t] >= SWEEP_MIN_ATR * a and c[t] >= bot - tol:
            out.append({"time": iso(df.index[t]), "close_time": iso(ctx.closes[t]), "side": "low", "level": bot,
                        "wick_extreme": float(l[t]), "close": float(c[t]), "pips_beyond": (bot - l[t]) / pip,
                        "session": session_label(ctx.closes[t])})
    out = sorted(out, key=lambda x: -x["pips_beyond"])[:limit]
    return sorted(out, key=lambda x: x["time"])


def swing_sweeps(ctx: Context, pip: float, lookback: int = 60, limit: int = 4) -> list[dict]:
    df = ctx.df
    out = []
    for e in ctx.run.events:
        if e.type in ("sweep_high", "sweep_low") and e.idx >= len(df) - lookback:
            out.append({"time": iso(df.index[e.idx]), "close_time": iso(ctx.closes[e.idx]),
                        "side": "high" if e.type == "sweep_high" else "low", "level": e.level,
                        "wick_extreme": e.wick, "close": e.close, "pips_beyond": abs(e.wick - e.level) / pip,
                        "session": session_label(ctx.closes[e.idx]), "of": f"swing {e.swing_label or ''}".strip()})
    return out[-limit:]


def read_timeframe(ctx: Context, pip: float, states: Optional[BoxState] = None,
                   fakeout_max: int = FAKEOUT_MAX) -> dict:
    df = ctx.df
    n = len(df)
    o, h, l, c = (df[x].to_numpy(float) for x in ("o", "h", "l", "c"))
    iv = ctx.interval
    st = states or box_states(ctx)
    end = n - 1
    last = float(c[end])
    atr = float(ctx.atr[end])
    swings = [{"label": s.label or s.kind, "price": s.price, "wick": s.wick, "time": iso(df.index[s.idx])}
              for s in ctx.run.swings[-6:]]
    sw_trend = ctx.run.trend[-1]
    imp = last_impulse(ctx, pip)
    out: dict = {"interval": iv, "last_close": last, "last_closed_candle_close": iso(ctx.closes[end]), "atr": atr,
                 "swing_trend": sw_trend, "swings": swings, "last_impulse": imp, "lead_in_impulse": None,
                 "box": None, "sweeps": [],
                 "source": ctx.source, "candles": n}
    status = int(st.status[end])
    if status != NONE:
        top, bot, s0 = float(st.top[end]), float(st.bot[end]), int(st.start[end])
        height = top - bot
        w = slice(max(s0, 0), n)
        wick_high, wick_low = float(h[w].max()), float(l[w].min())
        box = {"top": top, "bottom": bot, "mid": (top + bot) / 2, "height_pips": height / pip,
               "height_atr": height / atr if atr else None, "wick_high": wick_high, "wick_low": wick_low,
               "wick_high_time": iso(df.index[max(s0, 0) + int(np.argmax(h[w]))]),
               "wick_low_time": iso(df.index[max(s0, 0) + int(np.argmin(l[w]))]),
               "start": iso(df.index[max(s0, 0)]), "candles": int(n - max(s0, 0)), "state": _STATE[status]}
        if status == INSIDE:
            pct = (last - bot) / height * 100 if height > 0 else 50.0
            where = "near_top" if pct >= 75 else "near_bottom" if pct <= 25 else "mid_box"
            box["position"] = {"state": "inside", "pct_of_height": pct, "where": where,
                               "pips_to_top": (top - last) / pip, "pips_to_bottom": (last - bot) / pip}
        else:
            b = int(st.break_idx[end])
            edge = top if status == OUT_UP else bot
            beyond = abs(last - edge) / pip
            box["position"] = {"state": _STATE[status], "edge": edge, "pips_beyond": beyond,
                               "candles_ago": int(end - b), "break_candle_time": iso(df.index[b]),
                               "break_candle_close_time": iso(ctx.closes[b]), "break_close": float(c[b]),
                               "break_candle_open": float(o[b]),
                               "break_body_atr": float(abs(c[b] - o[b]) / ctx.atr_prev[b])
                               if np.isfinite(ctx.atr_prev[b]) else None,
                               "marginal": bool(abs(c[b] - edge) < ZONE_ATR * atr)}
        # last body close beyond each edge inside the window (beyond the break tolerance)
        tolv = np.array([BREAK_TOL_ATR * (ctx.atr[t - 1] if t > 0 and np.isfinite(ctx.atr[t - 1]) else 0.0)
                         for t in range(n)])
        rng = range(max(s0, 0), n)
        ca = [t for t in rng if c[t] > top + tolv[t]]
        cb = [t for t in rng if c[t] < bot - tolv[t]]
        near = [t for t in rng if top < c[t] <= top + tolv[t]]
        since = _t(df.index[max(s0, 0)])
        above_wick = [t for t in rng if c[t] > wick_high]
        box["closed_above_top"], box["closed_below_bottom"] = bool(ca), bool(cb)
        box["facts"] = [
            (f"no {iv} body has closed above {_px(top)} since {since}"
             + (f" (closest {_px(max(c[t] for t in near))}, inside the {BREAK_TOL_ATR} ATR tolerance)" if near else "")
             if not ca else f"last {iv} body close above {_px(top)}: {_px(c[ca[-1]])} at {_t(ctx.closes[ca[-1]])}"),
            (f"no {iv} body has closed below {_px(bot)} since {since}" if not cb else
             f"last {iv} body close below {_px(bot)}: {_px(c[cb[-1]])} at {_t(ctx.closes[cb[-1]])}"),
            (f"highest {iv} wick in the box {_px(wick_high)} ({_t(box['wick_high_time'])}); no {iv} body has closed "
             f"above that wick high" if not above_wick else
             f"a {iv} body closed above the wick high {_px(wick_high)}"),
        ]
        ep = st.episodes[int(st.episode[end])] if st.episode[end] >= 0 else None
        fo = [b for b in (ep["breaks"] if ep else []) if is_fakeout(b, fakeout_max)]
        box["fakeouts_above"] = sum(b["side"] > 0 for b in fo)
        box["fakeouts_below"] = sum(b["side"] < 0 for b in fo)
        box["lead_in_impulse"] = {1: "up", -1: "down", 0: "none"}[ep["lead_impulse"]] if ep else "none"
        out["lead_in_impulse"] = last_impulse(ctx, pip, upto_idx=max(ep["first_box_start"] if ep else s0, 0))
        if ep:
            box["first_box"] = {"top": ep["outer_top"], "bottom": ep["outer_bot"],
                                "start": iso(df.index[max(ep["first_box_start"], 0)]),
                                "note": "the first (wider) box of this consolidation, before newer swings "
                                        "narrowed it"}
        out["box"] = box
        out["sweeps"] = box_sweeps(ctx, top, bot, s0, pip)
        out["fakeouts"] = [public_fakeout(ctx, b, pip) for b in fo][-6:]
    else:
        out["sweeps"] = swing_sweeps(ctx, pip)
        out["fakeouts"] = []
    out["history"] = box_history(ctx, st, pip, fakeout_max)
    out["prior_box_edges"] = prior_box_edges(ctx, st, last, pip, atr)
    # regime
    fresh = imp is not None and imp["candles_since_end"] <= IMPULSE_FRESH
    if fresh and (status in (NONE, OUT_UP, OUT_DOWN) or imp["in_progress"]):
        regime = f"impulse_{imp['direction']}"
    elif status != NONE:
        regime = "consolidation"
    elif sw_trend in ("up", "down"):
        regime = f"trend_{sw_trend}"
    else:
        regime = "consolidation"
    out["regime"] = regime
    out["continuation"] = continuation(out, iv, pip)
    return out


def continuation(tf: dict, iv: str, pip: float) -> dict:
    """What must print to continue up / down, in break -> higher low / lower high terms."""
    box = tf.get("box")
    sw = tf["swings"]
    hi = next((s for s in reversed(sw) if s["label"] in ("HH", "LH", "EQH", "H")), None)
    lo = next((s for s in reversed(sw) if s["label"] in ("HL", "LL", "EQL", "L")), None)
    if box:
        top, bot, wh, wl = box["top"], box["bottom"], box["wick_high"], box["wick_low"]
        st = box["position"]["state"]
        if st == "inside":
            up = (f"a {iv} BODY close above {_px(top)} — and above the wick high {_px(wh)} to be a clean higher high — "
                  f"then a higher low that holds above {_px(top)}; a wick above without the close is only a sweep")
            dn = (f"a {iv} BODY close below {_px(bot)}, then a retest of {_px(bot)} from below that rejects (lower "
                  f"high under it); a wick below {_px(bot)} that closes back inside is only a sweep")
        elif st == "closed_below":
            p = box["position"]
            dn = (f"already printed: {iv} body closed {_px(p['break_close'])}, below {_px(bot)} "
                  f"({_t(p['break_candle_close_time'])}). Next: a retest of {_px(bot)} from below that closes back "
                  f"under it (lower high), then a new lower low below {_px(min(wl, tf['last_close']))}")
            up = (f"a {iv} body close back above {_px(bot)} cancels the break (failed breakdown → range again, "
                  f"opposite edge {_px(top)}); a real up-move still needs a body close above {_px(top)} / {_px(wh)}")
        else:
            p = box["position"]
            up = (f"already printed: {iv} body closed {_px(p['break_close'])}, above {_px(top)} "
                  f"({_t(p['break_candle_close_time'])}). Next: a retest of {_px(top)} from above that holds (higher "
                  f"low), then a new higher high above {_px(max(wh, tf['last_close']))}")
            dn = (f"a {iv} body close back below {_px(top)} cancels the break (failed breakout → range again, "
                  f"opposite edge {_px(bot)}); a real down-move still needs a body close below {_px(bot)}")
        return {"up": up, "down": dn}
    if hi and lo:
        return {"up": f"a {iv} body close above the last swing high {_px(hi['price'])} (new higher high), then a "
                      f"pullback that holds a higher low above {_px(lo['price'])}",
                "down": f"a {iv} body close below the last swing low {_px(lo['price'])} (new lower low), then a "
                        f"lower high that stays under {_px(hi['price'])}"}
    return {"up": "not enough confirmed swings", "down": "not enough confirmed swings"}


# ------------------------------------------------------------------ odds (history) + shuffled baseline

def box_events(ctx: Context, st: BoxState, pip: float, tp_pips: float, horizon: int = ODDS_HORIZON) -> dict:
    """Outcomes of (a) edge fades, (b) sweep-and-close-back, (c) body closes outside the box. The box used for
    candle t is the one known after candle t-1."""
    df = ctx.df
    n = len(df)
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    atr = ctx.atr
    res = {k: [] for k in ("edge_fade_to_mid", "sweep_to_mid", "sweep_to_opposite_edge", "break_continues_tp",
                           "break_continues_1atr")}
    for t in range(2, n):
        if st.status[t - 1] != INSIDE:
            continue
        top, bot = st.top[t - 1], st.bot[t - 1]
        a = atr[t - 1]
        if not np.isfinite(a) or a <= 0 or not np.isfinite(top):
            continue
        height = top - bot
        mid = (top + bot) / 2
        tol = BREAK_TOL_ATR * a
        zw = zone_width(height, a)
        last = min(n, t + horizon + 1)
        complete = t + horizon < n
        for side in (1, -1):  # 1 = top edge, -1 = bottom edge
            edge = top if side > 0 else bot
            far = h if side > 0 else l  # extreme toward the edge
            back = l if side > 0 else h  # extreme toward mid / opposite edge
            opp = bot if side > 0 else top
            reach = side * (far[t] - edge)  # > 0 beyond the edge
            closed_beyond = side * (c[t] - edge) > tol
            # (c) body close outside
            if closed_beyond:
                tgt_p, tgt_a, out_p, out_a = c[t] + side * tp_pips * pip, c[t] + side * a, None, None
                for j in range(t + 1, last):
                    if out_p is None and side * (far[j] - tgt_p) >= 0:
                        out_p = 1
                    if out_a is None and side * (far[j] - tgt_a) >= 0:
                        out_a = 1
                    if side * (c[j] - edge) <= 0:  # closed back inside
                        out_p = 0 if out_p is None else out_p
                        out_a = 0 if out_a is None else out_a
                    if out_p is not None and out_a is not None:
                        break
                if out_p is not None:
                    res["break_continues_tp"].append(out_p)
                if out_a is not None:
                    res["break_continues_1atr"].append(out_a)
                continue
            # (b) sweep: wick beyond, body did not close beyond (within the break tolerance)
            if reach > tol:
                ext = far[t]
                om = oo = None
                for j in range(t + 1, last):
                    if side * (c[j] - ext) > 0:  # body close beyond the wick extreme
                        om = 0 if om is None else om
                        oo = 0 if oo is None else oo
                        break
                    if om is None and side * (back[j] - mid) <= 0:
                        om = 1
                    if oo is None and side * (back[j] - opp) <= 0:
                        oo = 1
                    if om is not None and oo is not None:
                        break
                if om is not None:
                    res["sweep_to_mid"].append(om)
                if oo is not None:
                    res["sweep_to_opposite_edge"].append(oo)
            # (a) first arrival in the edge zone
            in_zone = side * (far[t] - (edge - side * zw)) >= 0
            was = side * (far[t - 1] - (edge - side * zw)) >= 0
            if in_zone and not was:
                o_ = None
                for j in range(t, last):
                    if side * (c[j] - edge) > tol:
                        o_ = 0
                        break
                    if j > t and side * (back[j] - mid) <= 0:
                        o_ = 1
                        break
                if o_ is not None:
                    res["edge_fade_to_mid"].append(o_)
        _ = complete
    return res


def break_events(ctx: Context, st: BoxState, pip: float, fakeout_max: int = FAKEOUT_MAX,
                 horizon: int = ODDS_HORIZON) -> tuple[dict, list[float]]:
    """(d) fakeout vs real break with splits, (e) what a fakeout leads to, (f) retests of real breaks.
    Returns (outcome lists, retest wick depths in pips)."""
    from .levels import session_of
    df = ctx.df
    n = len(df)
    h, l = df["h"].to_numpy(float), df["l"].to_numpy(float)
    res: dict[str, list] = {}
    depths: list[float] = []

    def add(key: str, v: int) -> None:
        res.setdefault(key, []).append(v)
    for b in st.breaks:
        t, side = b["idx"], b["side"]
        if t + fakeout_max >= n:
            continue  # not yet known whether it comes back inside
        fake = is_fakeout(b, fakeout_max)
        add("break_is_fakeout", int(fake))
        rel = "no_impulse" if b["imp_dir"] == 0 else "with_impulse" if b["imp_dir"] == side else "against_impulse"
        add(f"break_is_fakeout|lead_in={rel}", int(fake))
        add(f"break_is_fakeout|attempt={'first' if b['attempt'] == 1 else 'later'}", int(fake))
        add(f"break_is_fakeout|session={session_of(ctx.closes[t] - pd.Timedelta(minutes=1))}", int(fake))
        if np.isfinite(b["body_atr"]):
            add(f"break_is_fakeout|body={'>=1 ATR' if b['body_atr'] >= 1.0 else '<1 ATR'}", int(fake))
        if fake:
            f = fakeout_followed(ctx, b, horizon)
            if f["complete"] or f["reached_opposite_edge"]:
                add("fakeout_reaches_opposite_edge", int(f["reached_opposite_edge"]))
            if f["complete"] or f["broke_opposite_side"]:
                add("fakeout_then_breaks_opposite_side", int(f["broke_opposite_side"]))
        elif t + RETEST_WINDOW < n:
            edge = b["edge"]
            back = l if side > 0 else h
            tol = BREAK_TOL_ATR * ctx.atr[t] if np.isfinite(ctx.atr[t]) else 0.0
            stop = min(t + RETEST_WINDOW, (b["back_idx"] - 1) if b["back_idx"] is not None else n)
            touched, depth = False, 0.0
            for j in range(t + 1, stop + 1):
                if side * (back[j] - edge) <= tol:
                    touched = True
                    depth = max(depth, side * (edge - back[j]))
            add("real_break_retests_edge", int(touched))
            if touched:
                depths.append(max(depth, 0.0) / pip)
    return res, depths


def _stat(outcomes: list[int]) -> dict:
    n = len(outcomes)
    if n == 0:
        return {"n": 0, "rate": None, "ci95": [None, None]}
    w = int(sum(outcomes))
    lo, hi = wilson_ci(w, n)
    return {"n": n, "rate": w / n, "ci95": [lo, hi]}


def compare(real: dict, null: Optional[dict]) -> dict:
    out = dict(real)
    if not null or not null.get("n") or not real.get("n"):
        out.update(baseline_rate=None, verdict="insufficient data" if real.get("n", 0) < MIN_N else "no baseline")
        return out
    p1, n1, p2, n2 = real["rate"], real["n"], null["rate"], null["n"]
    pool = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = (pool * (1 - pool) * (1 / n1 + 1 / n2)) ** 0.5
    z = (p1 - p2) / se if se > 0 else 0.0
    if n1 < MIN_N or n2 < MIN_N:
        v = "insufficient data"
    elif z >= NULL_Z:
        v = "higher than random candles"
    elif z <= -NULL_Z:
        v = "lower than random candles"
    else:
        v = "no edge (same as random candles)"
    out.update(baseline_rate=p2, baseline_n=n2, z=round(z, 2), verdict=v)
    return out


ODDS_LABELS_EXTRA = {
    "break_is_fakeout": "(d) a body close outside the box is back inside within N candles (fakeout, not a break)",
    "fakeout_reaches_opposite_edge": "(e) after a fakeout of one side → price reaches the OPPOSITE edge",
    "fakeout_then_breaks_opposite_side": "(e) after a fakeout of one side → a body closes beyond the OPPOSITE side",
    "real_break_retests_edge": "(f) real break → price comes back to the broken edge within 24 candles",
}
ODDS_LABELS = {
    "edge_fade_to_mid": "(a) range-edge fade: price inside the box reaches an edge zone → reaches mid before a body "
                        "close beyond the edge",
    "sweep_to_mid": "(b) sweep-and-close-back of an edge → reaches mid before a body close beyond the wick extreme",
    "sweep_to_opposite_edge": "(b) sweep-and-close-back → reaches the OPPOSITE edge before a close beyond the wick",
    "break_continues_tp": "(c) body close outside the box → runs tp_pips further before a body closes back inside",
    "break_continues_1atr": "(c) body close outside the box → runs 1 ATR further before a body closes back inside",
}


def timeframe_odds(ctx: Context, pip: float, tp_pips: float, shuffles: int = 2,
                   fakeout_max: int = FAKEOUT_MAX) -> dict:
    st = box_states(ctx)
    real = box_events(ctx, st, pip, tp_pips)
    real_b, depths = break_events(ctx, st, pip, fakeout_max)
    real.update(real_b)
    pooled: dict[str, list] = {}
    null_depths: list[float] = []
    try:
        for seed in range(shuffles):
            sh = shuffled_base(ctx.df, seed + 1)
            dur = pd.Timedelta(hours=INTERVALS[ctx.interval][4])
            sctx = context_from_frame(None, ctx.interval, sh, sh.index + dur)
            sst = box_states(sctx)
            ev = box_events(sctx, sst, pip, tp_pips)
            evb, nd = break_events(sctx, sst, pip, fakeout_max)
            ev.update(evb)
            null_depths += nd
            for k, v in ev.items():
                pooled.setdefault(k, []).extend(v)
    except Exception:  # pragma: no cover
        pooled = {}
    main, splits = {}, {}
    for k, v in real.items():
        row = compare(_stat(v), _stat(pooled.get(k, [])) if pooled else None)
        if "|" in k:
            splits[k.split("|", 1)[1]] = row
        else:
            main[k] = {"what": {**ODDS_LABELS, **ODDS_LABELS_EXTRA}.get(k, k), **row}
    d = np.array(depths) if depths else None
    nd = np.array(null_depths) if null_depths else None
    return {"interval": ctx.interval, "candles": len(ctx.df), "period_start": iso(ctx.df.index[0]),
            "period_end": iso(ctx.closes[-1]), "source": ctx.source, "horizon_candles": ODDS_HORIZON,
            "tp_pips": tp_pips, "fakeout_max": fakeout_max, "boxes_completed": sum(e["end_idx"] is not None
                                                                                   for e in st.episodes),
            "events": main, "fakeout_splits": splits,
            "retest_depth_pips": {"n": len(depths), "median": float(np.median(d)) if d is not None else None,
                                  "p80": float(np.percentile(d, 80)) if d is not None else None,
                                  "baseline_median": float(np.median(nd)) if nd is not None else None,
                                  "what": "how far the retest wick went back PAST the broken edge (real breaks that "
                                          "were retested)"},
            "baseline": f"same rules on the same candles shuffled within each hour of the day ({shuffles} runs, "
                        f"pooled); verdict needs |z| >= {NULL_Z} and n >= {MIN_N}"}


# ------------------------------------------------------------------ stack + playbook

def _imp_txt(imp: dict) -> str:
    return (f"impulsive {'drop' if imp['direction'] == 'down' else 'rally'} {_px(imp['from'])} → {_px(imp['to'])} "
            f"({imp['pips']:.0f} pips, {imp['candles']} candles, ended {_t(imp['end'])})")


def _regime_phrase(tf: dict, pip: float) -> str:
    iv, rg, box = tf["interval"], tf["regime"], tf.get("box")
    imp, lead = tf.get("last_impulse"), tf.get("lead_in_impulse")
    parts = []
    if box:
        if lead:
            parts.append(_imp_txt(lead))
        p = box["position"]
        if p["state"] == "inside":
            parts.append(f"consolidation {_px(box['bottom'])}–{_px(box['top'])} ({box['height_pips']:.0f} pips, "
                         f"{box['candles']} candles), price at {p['pct_of_height']:.0f}% of the box")
        else:
            side = "BELOW" if p["state"] == "closed_below" else "ABOVE"
            parts.append(f"range {_px(box['bottom'])}–{_px(box['top'])} BROKEN: body closed {side} {_px(p['edge'])} "
                         f"{p['candles_ago']} candle(s) ago, now {p['pips_beyond']:.0f} pips beyond")
            if imp and rg.startswith("impulse") and (not lead or imp["end"] != lead["end"]):
                parts.append("by an " + _imp_txt(imp))
        return f"{iv}: " + ", then ".join(parts[:2]) + (" " + parts[2] if len(parts) > 2 else "")
    if imp and rg.startswith("impulse"):
        parts.append(_imp_txt(imp))
    elif rg.startswith("trend"):
        parts.append(f"{rg.replace('_', ' ')} by body swings ("
                     + " ".join(f"{s['label']} {_px(s['price'])}" for s in tf["swings"][-3:]) + ")")
    else:
        parts.append("no clean structure (mixed swings)")
    return f"{iv}: " + "; ".join(parts)


def build_stack(tfs: dict[str, dict], order: list[str], pip: float) -> dict:
    """order = intervals high -> low (only those with data)."""
    ctx_iv = order[0]
    trig_iv = order[-1]
    setup_iv = order[1] if len(order) >= 3 else (order[0] if len(order) < 2 else order[-1] if len(order) == 2 else order[1])
    if len(order) == 2:
        setup_iv = order[0]
    roles = {"context": ctx_iv, "setup": setup_iv, "trigger": trig_iv}
    lower = [iv for iv in order if INTERVALS[iv][4] < INTERVALS[setup_iv][4]]
    ctx, setup = tfs[ctx_iv], tfs[setup_iv]
    lines = [_regime_phrase(tfs[iv], pip) for iv in order]
    pb_tf = _playbook_box(ctx, setup)
    hi_box = pb_tf["box"] if pb_tf else None
    box_iv = pb_tf["interval"] if pb_tf else None
    lower_trending = [iv for iv in lower if tfs[iv]["regime"].startswith(("trend", "impulse"))]
    if hi_box and hi_box["position"]["state"] == "inside" and lower_trending:
        note = (f"{'/'.join(lower_trending)} swings are trend-like but they sit inside the {box_iv} range "
                f"{_px(hi_box['bottom'])}–{_px(hi_box['top'])}: that is noise until an edge is reached.")
    elif hi_box and hi_box["position"]["state"] != "inside":
        e = hi_box["position"]["edge"]
        note = (f"The {box_iv} range is broken by a body close, so the lower timeframes only matter at the retest of "
                f"{_px(e)}: a rejection there continues the break, a body close back through cancels it.")
    elif not hi_box and ctx["regime"].split("_")[-1] != setup["regime"].split("_")[-1] and \
            ctx["regime"] != "consolidation" and setup["regime"] != "consolidation":
        note = f"{ctx_iv} and {setup_iv} disagree on direction — no top-down alignment."
    else:
        note = "Timeframes are read top-down: context sets the direction, setup the level, trigger the entry."
    return {"context": {"interval": ctx_iv, "regime": ctx["regime"]},
            "setup": {"interval": setup_iv, "regime": setup["regime"]},
            "trigger": {"interval": trig_iv, "regime": tfs[trig_iv]["regime"],
                        "also": [iv for iv in lower if iv != trig_iv]},
            "roles": roles, "reading": " ".join(x + "." for x in lines) + " " + note}


def _targets(edge: float, d: int, pip: float, tp_pips: float, tp2_pips: float, cap1: Optional[float],
             cap2: Optional[float], n1: str, n2: str) -> list[dict]:
    out = []
    for name, tp, cap, cname in (("tp1", tp_pips, cap1, n1), ("tp2", tp2_pips, cap2, n2)):
        dist = tp * pip
        capped = False
        if cap is not None and abs(cap - edge) < dist:
            dist, capped = abs(cap - edge), True
        out.append({"name": name, "price": edge + d * dist, "pips": dist / pip,
                    "capped_at": cname if capped else None})
    return out


def _zone(side: str, tf: dict, pip: float, sl_pips: float, tp_pips: float, tp2_pips: float, trig: str,
          setup_iv: str) -> dict:
    box = tf["box"]
    top, bot, mid = box["top"], box["bottom"], box["mid"]
    zw = zone_width(top - bot, tf["atr"])
    sell = side == "sell"
    edge, d = (top, -1) if sell else (bot, 1)
    wick = box["wick_high"] if sell else box["wick_low"]
    stop = edge - d * sl_pips * pip
    inside = (wick > stop) if sell else (wick < stop)
    word = "above" if sell else "below"
    return {
        "side": "short" if sell else "long", "low": edge - zw if sell else edge, "high": edge if sell else edge + zw,
        "edge": edge, "stop": stop, "stop_pips": sl_pips, "wick_extreme": wick,
        "wick_beyond_edge_pips": abs(wick - edge) / pip, "stop_inside_prior_sweeps": bool(inside),
        "stop_note": (f"prior wicks reached {_px(wick)}, {abs(wick - edge) / pip:.0f} pips {word} the edge: a "
                      f"{sl_pips:g}-pip stop at {_px(stop)} sits INSIDE earlier sweeps (it would have been hit); a stop "
                      f"beyond the wick needs ≥{abs(wick - edge) / pip + 5:.0f} pips" if inside else
                      f"stop {_px(stop)} is beyond the furthest prior wick {_px(wick)}"),
        "targets": _targets(edge, d, pip, tp_pips, tp2_pips, mid, bot if sell else top, "box mid", "opposite edge"),
        "trigger": (f"wait for a {trig} candle that pushes into {_px(edge - zw if sell else edge)}–"
                    f"{_px(edge if sell else edge + zw)} (a wick {word} {_px(edge)} is fine) and CLOSES back "
                    f"{'below' if sell else 'above'} {_px(edge)}; enter on that rejection close"),
        "invalidation": f"a {setup_iv} body close {word} {_px(edge)} (range edge broken → switch to break/retest)",
    }


STALE_BREAK_CANDLES, STALE_BREAK_ATR = 12, 3.0


def _usable(tf: dict) -> bool:
    """A box is usable for the plan while price is inside it, or just after a body close outside it."""
    box = tf.get("box")
    if not box:
        return False
    p = box["position"]
    if p["state"] == "inside":
        return True
    return p["candles_ago"] <= STALE_BREAK_CANDLES and abs(tf["last_close"] - p["edge"]) <= STALE_BREAK_ATR * tf["atr"]


def _playbook_box(ctx: dict, setup: dict) -> Optional[dict]:
    """Highest timeframe whose box is still actionable (context first, then setup)."""
    if _usable(ctx):
        return ctx
    if setup is not ctx and _usable(setup):
        return setup
    return None


def build_playbook(tfs: dict[str, dict], stack: dict, pip: float, sl_pips: float, tp_pips: float,
                   tp2_pips: float) -> dict:
    ctx_iv, setup_iv, trig_iv = (stack[k]["interval"] for k in ("context", "setup", "trigger"))
    ctx, setup = tfs[ctx_iv], tfs[setup_iv]
    trig = "/".join(dict.fromkeys(stack["trigger"]["also"] + [trig_iv]))
    box_tf = _playbook_box(ctx, setup)
    params = {"sl_pips": sl_pips, "tp_pips": tp_pips, "tp2_pips": tp2_pips, "pip": pip}
    if box_tf is None:
        cd, sd = ctx["regime"].split("_")[-1], setup["regime"].split("_")[-1]
        if cd in ("up", "down") and sd in (cd, "consolidation"):
            up = cd == "up"
            ref = tfs[setup_iv] if setup["swings"] else ctx
            lo = next((s for s in reversed(ref["swings"]) if s["label"] in ("HL", "LL", "L", "EQL")), None)
            hi = next((s for s in reversed(ref["swings"]) if s["label"] in ("HH", "LH", "H", "EQH")), None)
            prot = lo if up else hi
            if prot:
                d = 1 if up else -1
                zw = ZONE_ATR * ref["atr"]
                level = prot["price"]
                return {"mode": "trend_pullback", "timeframe": ref["interval"], "direction": "long" if up else "short",
                        "pullback_zone": {"low": level if up else level - zw, "high": level + zw if up else level,
                                          "level": level, "label": prot["label"]},
                        "stop": level - d * sl_pips * pip, "stop_pips": sl_pips,
                        "targets": _targets(level, d, pip, tp_pips, tp2_pips, None, None, "", ""),
                        "trigger": f"wait for a {trig} rejection close at the pullback zone in the trend direction",
                        "invalidation": f"a {ref['interval']} body close {'below' if up else 'above'} {_px(level)} "
                                        f"(the protected {'higher low' if up else 'lower high'})",
                        "params": params}
        return {"mode": "stand_aside", "why": f"no box on {ctx_iv}/{setup_iv} and no aligned trend "
                                              f"({ctx_iv} {ctx['regime']}, {setup_iv} {setup['regime']})",
                "params": params}
    box = box_tf["box"]
    iv = box_tf["interval"]
    p = box["position"]
    if p["state"] == "inside":
        mid_lo = box["bottom"] + 0.3 * (box["top"] - box["bottom"])
        mid_hi = box["top"] - 0.3 * (box["top"] - box["bottom"])
        return {"mode": "range", "timeframe": iv, "box": {k: box[k] for k in ("top", "bottom", "mid", "wick_high",
                                                                              "wick_low", "height_pips")},
                "price_position": p["where"], "pct_of_height": p["pct_of_height"],
                "sell_zone": _zone("sell", box_tf, pip, sl_pips, tp_pips, tp2_pips, trig, iv),
                "buy_zone": _zone("buy", box_tf, pip, sl_pips, tp_pips, tp2_pips, trig, iv),
                "no_trade_zone": {"low": mid_lo, "high": mid_hi, "note": "mid-box: no trade, wait for an edge"},
                "params": params}
    down = p["state"] == "closed_below"
    edge = p["edge"]
    d = -1 if down else 1
    zw = zone_width(box["top"] - box["bottom"], box_tf["atr"])
    h_since = box["wick_low"] if down else box["wick_high"]
    mm = edge + d * (box["top"] - box["bottom"])
    last = box_tf["last_close"]
    dist = abs(last - edge) / pip
    return {
        "mode": "break_retest", "timeframe": iv, "direction": "short" if down else "long",
        "broken_edge": edge, "box": {k: box[k] for k in ("top", "bottom", "mid", "wick_high", "wick_low",
                                                         "height_pips")},
        "break": {"close": p["break_close"], "candle_close_time": p["break_candle_close_time"],
                  "candles_ago": p["candles_ago"], "pips_beyond_now": p["pips_beyond"], "marginal": p["marginal"]},
        "retest_zone": {"low": edge - zw if down else edge - 0.25 * zw, "high": edge + 0.25 * zw if down else edge + zw},
        "distance_to_retest_pips": dist,
        "stop": edge - d * sl_pips * pip, "stop_pips": sl_pips,
        "targets": _targets(edge, d, pip, tp_pips, tp2_pips, None, None, "", "")
        + [{"name": "measured_move", "price": mm, "pips": abs(mm - edge) / pip, "capped_at": None}],
        "extreme_since_break": h_since,
        "trigger": (f"wait for price to come back to {_px(edge)} from {'below' if down else 'above'}; enter "
                    f"{'short' if down else 'long'} when a {trig} candle tags the zone and CLOSES back "
                    f"{'below' if down else 'above'} {_px(edge)}"),
        "cancel": (f"a {iv} body close back {'above' if down else 'below'} {_px(edge)} = failed break, back inside the "
                   f"range (then the opposite edge {_px(box['top'] if down else box['bottom'])} is the magnet)"),
        "params": params,
    }


def _risk_text(odds: Optional[dict], sl_pips: float) -> str:
    if not odds:
        return "historical odds not attached"
    ev, rd = odds["events"], odds["retest_depth_pips"]
    parts = []
    f = ev.get("break_is_fakeout")
    if f and f["n"]:
        parts.append(f"{f['rate'] * 100:.0f}% of {odds['interval']} body closes outside a box were back inside within "
                     f"{odds['fakeout_max']} candles (n={f['n']}; random candles {_pct(f.get('baseline_rate'))})")
    r = ev.get("real_break_retests_edge")
    if r and r["n"]:
        parts.append(f"{r['rate'] * 100:.0f}% of real breaks came back to the edge (n={r['n']})")
    if rd.get("n"):
        parts.append(f"retest wicks went {rd['median']:.0f} pips (median) / {rd['p80']:.0f} pips (p80) past the edge, "
                     f"so a {sl_pips:g}-pip stop is {'inside' if rd['median'] > sl_pips else 'around'} normal retest "
                     f"noise")
    return "; ".join(parts) if parts else "not enough history"


def build_cases(tfs: dict[str, dict], stack: dict, pb: dict, pip: float, odds: Optional[dict] = None) -> list[dict]:
    """Explicit ordered if-then list: sell / buy / hold with exact prices."""
    ctx_iv, setup_iv, trig_iv = (stack[k]["interval"] for k in ("context", "setup", "trigger"))
    trig = "/".join(dict.fromkeys(stack["trigger"]["also"] + [trig_iv]))
    P = pb["params"]
    sl, tp, tp2 = P["sl_pips"], P["tp_pips"], P["tp2_pips"]
    m = pb["mode"]
    risk = _risk_text(odds, sl)

    def case(action, when, entry=None, entry_price=None, stop=None, targets=None, reason="", rk=None):
        return {"action": action, "when": when, "entry": entry, "entry_price": entry_price, "stop": stop,
                "targets": targets or [], "trigger_tf": trig if action != "hold" else None, "reason": reason,
                "risk": rk}
    if m == "break_retest":
        iv, E = pb["timeframe"], pb["broken_edge"]
        down = pb["direction"] == "short"
        d = -1 if down else 1
        z, box = pb["retest_zone"], pb["box"]
        last = tfs[iv]["last_close"]
        act, opp_act = ("sell", "buy") if down else ("buy", "sell")
        side, oside = ("below", "above") if down else ("above", "below")
        flip_iv = setup_iv if INTERVALS[setup_iv][4] < INTERVALS[iv][4] else iv
        tg = [dict(t) for t in pb["targets"]]
        imp = tfs[ctx_iv].get("lead_in_impulse") or tfs[ctx_iv].get("last_impulse")
        if imp and (imp["direction"] == "down") == down and d * (imp["to"] - E) > 0:
            tg.append({"name": "prior_impulse_extreme", "price": imp["to"], "pips": abs(imp["to"] - E) / pip,
                       "capped_at": None})
        tg = sorted(tg, key=lambda x: x["pips"])
        opp_edge = box["top"] if down else box["bottom"]
        return [
            case(act, f"price comes back to {_px(E)} from {side}: a {trig} candle trades into {_px(z['low'])}–"
                      f"{_px(z['high'])} (a wick through {_px(E)} is normal) and its BODY closes back {side} {_px(E)}",
                 f"at the close of that rejection candle, near {_px(E)}", E, E - d * sl * pip, tg,
                 f"the {iv} body closed {side} the range {'bottom' if down else 'top'} {_px(E)}; a rejected retest "
                 f"turns the old {'support into resistance' if down else 'resistance into support'}",
                 f"stop {_px(E - d * sl * pip)} = {sl:g} pips beyond the edge; {risk}"),
            case("hold", f"price stays {side} {_px(E)} without coming back to it (now: {_px(last)}, "
                         f"{pb['distance_to_retest_pips']:.0f} pips {side} the edge)",
                 reason=f"no retest = no entry; a {act} here is chasing with the stop far from the level"),
            case("hold", f"price runs to {_px(tg[-1]['price'])} (prior extreme / measured move) without ever "
                         f"retesting {_px(E)}",
                 reason="the move already ran without you; wait for the next lower high / new box, do not chase"
                 if down else "the move already ran without you; wait for the next higher low / new box"),
            case(opp_act, f"a {flip_iv} BODY closes back {oside} {_px(E)} (inside the box again) — the break was a "
                          f"fakeout",
                 f"on the retest of {_px(E)} from {oside} that holds (rejection close {oside} it)", E,
                 E + d * sl * pip,
                 [{"name": "box_mid", "price": box["mid"], "pips": abs(box["mid"] - E) / pip, "capped_at": None},
                  {"name": "opposite_edge", "price": opp_edge, "pips": abs(opp_edge - E) / pip, "capped_at": None}],
                 f"a body close back inside cancels the break; fakeouts send price toward the opposite edge "
                 f"{_px(opp_edge)}",
                 _fake_text(odds)),
            case("hold", f"after such a flip, price in the middle of the box ({_px(box['bottom'] + 0.3 * (box['top'] - box['bottom']))}"
                         f"–{_px(box['top'] - 0.3 * (box['top'] - box['bottom']))})",
                 reason="mid-box is no trade; only the edges give a defined stop"),
        ]
    if m == "range":
        iv, box = pb["timeframe"], pb["box"]
        s, b, nt = pb["sell_zone"], pb["buy_zone"], pb["no_trade_zone"]
        return [
            case("sell", f"price is in {_px(s['low'])}–{_px(s['high'])} and a {trig} candle wicks up (even above "
                         f"{_px(s['edge'])}) and its BODY closes back below {_px(s['edge'])}",
                 f"at the close of that rejection candle, near {_px(s['edge'])}", s["edge"], s["stop"], s["targets"],
                 f"{iv} range top by bodies; no {iv} body has closed above it", s["stop_note"]),
            case("buy", f"price is in {_px(b['low'])}–{_px(b['high'])} and a {trig} candle wicks down (even below "
                        f"{_px(b['edge'])}) and its BODY closes back above {_px(b['edge'])}",
                 f"at the close of that rejection candle, near {_px(b['edge'])}", b["edge"], b["stop"], b["targets"],
                 f"{iv} range bottom by bodies; no {iv} body has closed below it", b["stop_note"]),
            case("hold", f"price between {_px(nt['low'])} and {_px(nt['high'])} (mid-box)",
                 reason="middle of the range: no edge, no defined stop"),
            case("buy", f"a {iv} BODY closes above {_px(box['top'])} (close-confirmed break), then price retests "
                        f"{_px(box['top'])} from above and a {trig} candle closes back above it",
                 f"on that retest rejection, near {_px(box['top'])}", box["top"], box["top"] - sl * pip,
                 _targets(box["top"], 1, pip, tp, tp2, None, None, "", ""),
                 "range top broken by a body close: resistance becomes support", f"{risk}"),
            case("sell", f"a {iv} BODY closes below {_px(box['bottom'])} (close-confirmed break), then price retests "
                         f"{_px(box['bottom'])} from below and a {trig} candle closes back below it",
                 f"on that retest rejection, near {_px(box['bottom'])}", box["bottom"], box["bottom"] + sl * pip,
                 _targets(box["bottom"], -1, pip, tp, tp2, None, None, "", ""),
                 "range bottom broken by a body close: support becomes resistance", f"{risk}"),
        ]
    if m == "trend_pullback":
        z = pb["pullback_zone"]
        act = "buy" if pb["direction"] == "long" else "sell"
        return [
            case(act, f"price pulls back into {_px(z['low'])}–{_px(z['high'])} and a {trig} candle closes back in the "
                      f"trend direction", f"at that rejection close, near {_px(z['level'])}", z["level"], pb["stop"],
                 pb["targets"], f"{pb['timeframe']} trend intact while the {z['label']} holds", None),
            case("hold", "price is extended away from the pullback zone", reason="no pullback = no entry"),
            case("hold", pb["invalidation"], reason="structure broken: stand aside until a new box or trend forms"),
        ]
    return [case("hold", pb.get("why", "no setup"), reason="no box and no aligned trend on the higher timeframes")]


def _fake_text(odds: Optional[dict]) -> Optional[str]:
    if not odds:
        return None
    e = odds["events"].get("fakeout_reaches_opposite_edge")
    if not e or not e["n"]:
        return None
    return (f"after a fakeout, price reached the opposite edge {e['rate'] * 100:.0f}% of the time on {odds['interval']} "
            f"(n={e['n']}; random candles {_pct(e.get('baseline_rate'))} → {e['verdict']})")


def build_reasons(tfs: dict[str, dict], stack: dict, playbook: dict, pip: float) -> list[str]:
    out = []
    for role in ("context", "setup"):
        tf = tfs[stack[role]["interval"]]
        iv = tf["interval"]
        if role == "setup" and iv == stack["context"]["interval"]:
            continue
        if tf.get("box") and not _usable(tf):
            p = tf["box"]["position"]
            out.append(f"{iv}: the old range {_px(tf['box']['bottom'])}–{_px(tf['box']['top'])} was left "
                       f"{p['candles_ago']} candles ago and price is {p['pips_beyond']:.0f} pips away from "
                       f"{_px(p['edge'])} — too far to plan a retest; {iv} is {tf['regime'].replace('_', ' ')}.")
            continue
        box = tf.get("box")
        imp = tf.get("lead_in_impulse") if box else tf.get("last_impulse")
        if imp:
            out.append(f"{iv}: impulsive {'drop' if imp['direction'] == 'down' else 'rally'} from {_px(imp['from'])} to "
                       f"{_px(imp['to'])} ({imp['pips']:.0f} pips in {imp['candles']} candles, {_t(imp['start'])} → "
                       f"{_t(imp['end'])}).")
        if box:
            out.append(f"{iv}: {'then a ' if imp else ''}range by candle bodies — top {_px(box['top'])}, bottom {_px(box['bottom'])} "
                       f"({box['height_pips']:.0f} pips, {box['candles']} candles since {_t(box['start'])}); wicks reached "
                       f"{_px(box['wick_high'])} and {_px(box['wick_low'])}.")
            sw = [s for s in tf["sweeps"] if s["side"] == "high"] if role == "context" else []
            sl = [s for s in tf["sweeps"] if s["side"] == "low"] if role == "context" and \
                box["position"]["state"] == "inside" else []
            for name, lst, edge in (("top", sw, box["top"]), ("bottom", sl, box["bottom"])):
                big = sorted(lst, key=lambda s: -s["pips_beyond"])[:2]
                if big:
                    big = sorted(big, key=lambda s: s["time"])
                    out.append(f"{iv}: wicks swept the {name} {_px(edge)} — " + " and ".join(
                        f"{_px(s['wick_extreme'])} ({s['pips_beyond']:.0f} pips beyond, candle {_t(s['time'])}, closed "
                        f"{_px(s['close'])})" for s in big) + " — and each candle CLOSED back inside: sweeps, not breaks.")
            if role == "context":
                out.append(f"{iv}: {box['facts'][0]}; {box['facts'][2]}."
                           + (" No close above the prior wick high = no new higher high, which is why it did not "
                              "continue up." if not box["closed_above_top"] else ""))
            fresh = tf.get("last_impulse")
            if fresh and tf["regime"].startswith("impulse") and (not imp or fresh["end"] != imp["end"]):
                out.append(f"{iv}: the latest leg is an impulsive {'drop' if fresh['direction'] == 'down' else 'rally'} "
                           f"{_px(fresh['from'])} → {_px(fresh['to'])} ({fresh['pips']:.0f} pips in {fresh['candles']} "
                           f"candles, ended {_t(fresh['end'])}).")
            p = box["position"]
            if p["state"] != "inside":
                side = "below" if p["state"] == "closed_below" else "above"
                out.append(f"{iv}: the candle that closed {_t(p['break_candle_close_time'])} has a body "
                           f"{_px(p['break_candle_open'])} → {_px(p['break_close'])}: it CLOSED {side} the range "
                           f"{'bottom' if side == 'below' else 'top'} {_px(p['edge'])}"
                           + (" (marginally)" if p["marginal"] else "") + f"; last close {_px(tf['last_close'])} is "
                           f"{p['pips_beyond']:.0f} pips {side} it. The range edge is broken by a body close.")
        elif not imp:
            out.append(f"{iv}: {tf['regime'].replace('_', ' ')} — swings "
                       + " ".join(f"{s['label']} {_px(s['price'])}" for s in tf["swings"][-4:]) + ".")
    trig = tfs[stack["trigger"]["interval"]]
    out.append(f"{trig['interval']}: {trig['regime'].replace('_', ' ')} ("
               + " ".join(f"{s['label']} {_px(s['price'])}" for s in trig["swings"][-4:])
               + ") — used only to time the entry at the zone, not for direction.")
    m = playbook["mode"]
    if m == "range":
        s, b = playbook["sell_zone"], playbook["buy_zone"]
        out.append(f"Plan (range): sell {_px(s['low'])}–{_px(s['high'])}, buy {_px(b['low'])}–{_px(b['high'])}; price "
                   f"is {playbook['price_position'].replace('_', ' ')} ({playbook['pct_of_height']:.0f}% of the box)"
                   + (" → no trade until an edge is reached." if playbook["price_position"] == "mid_box" else "."))
    elif m == "break_retest":
        z = playbook["retest_zone"]
        out.append(f"Plan (break → retest): {playbook['direction']} only on a retest of {_px(playbook['broken_edge'])} "
                   f"(zone {_px(z['low'])}–{_px(z['high'])}, {playbook['distance_to_retest_pips']:.0f} pips away) that "
                   f"is rejected by a closing candle; {playbook['cancel']}.")
    elif m == "trend_pullback":
        z = playbook["pullback_zone"]
        out.append(f"Plan (trend pullback): {playbook['direction']} from {_px(z['low'])}–{_px(z['high'])}; "
                   f"{playbook['invalidation']}.")
    else:
        out.append(f"Plan: stand aside — {playbook['why']}.")
    return out


# ------------------------------------------------------------------ service

def analyze_mtf(ctxs: dict[str, Context], pip: float, sl_pips: float = 25, tp_pips: float = 50,
                tp2_pips: float = 100, fakeout_max: int = FAKEOUT_MAX) -> dict:
    """Pure: contexts of CLOSED candles per interval -> per-timeframe read, stack, playbook, reasons, cases."""
    order = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
    tfs = {iv: read_timeframe(ctxs[iv], pip, fakeout_max=fakeout_max) for iv in order}
    stack = build_stack(tfs, order, pip)
    playbook = build_playbook(tfs, stack, pip, sl_pips, tp_pips, tp2_pips)
    playbook["reasons"] = build_reasons(tfs, stack, playbook, pip)
    playbook["cases"] = build_cases(tfs, stack, playbook, pip)
    last = tfs[order[-1]]["last_close"]
    edges = []
    for iv in dict.fromkeys([stack["context"]["interval"], stack["setup"]["interval"]]):
        edges += tfs[iv]["prior_box_edges"]
    for e in edges:
        e["distance_pips"] = (e["level"] - last) / pip
        e["role"] = "resistance" if e["level"] > last else "support"
    edges = sorted(edges, key=lambda x: abs(x["distance_pips"]))[:6]
    return {"timeframes": tfs, "stack": stack, "playbook": playbook, "prior_box_edges": edges}


def mtf(sym: Symbol, intervals: Optional[list[str]] = None, pip: Optional[float] = None, sl_pips: float = 25,
        tp_pips: float = 50, tp2_pips: float = 100, fakeout_max: int = FAKEOUT_MAX) -> dict:
    ivs = list(dict.fromkeys(intervals or ["4h", "1h", "15m", "5m"]))
    if not 1 <= len(ivs) <= 5:
        raise ValueError("1 to 5 intervals")
    pip_used = pip or default_pip(sym)
    rk = replay_key()

    def load():
        now = now_ts()
        ctxs, needs = {}, []
        for iv in ivs:
            try:
                ctxs[iv] = build_context(sym, iv, now, max_bars=1_000_000)
                if len(ctxs[iv].run.swings) < 4:
                    needs.append({"interval": iv, "reason": "fewer than 4 confirmed swings"})
            except Exception as e:
                needs.append({"interval": iv, "reason": f"{type(e).__name__}: {str(e)[:160]}"})
        if not ctxs:
            from .data import DataError
            raise DataError(f"no data for {sym.id} on any of {ivs}: " + "; ".join(n["reason"] for n in needs))
        res = analyze_mtf(ctxs, pip_used, sl_pips, tp_pips, tp2_pips, fakeout_max)
        order = sorted(ctxs, key=lambda x: -INTERVALS[x][4])
        odds = {}
        for iv in [i for i in order if INTERVALS[i][4] >= 1.0][:2] or order[:1]:
            key = ("mtf_odds", sym.id, iv, pip_used, tp_pips, fakeout_max, rk[:10] if rk else None,
                   len(ctxs[iv].df) // 50)
            try:
                odds[iv] = cache.get_or_set(key, TTL_MODEL, lambda iv=iv: timeframe_odds(
                    ctxs[iv], pip_used, tp_pips, fakeout_max=fakeout_max))
            except Exception as e:
                needs.append({"interval": iv, "reason": f"odds failed: {type(e).__name__}: {str(e)[:120]}"})
        pb = res["playbook"]
        box_iv = pb.get("timeframe")
        pb["cases"] = build_cases(res["timeframes"], res["stack"], pb, pip_used,
                                  odds.get(box_iv) or next(iter(odds.values()), None))
        first = ctxs[order[0]]
        info = first.source_info or {"source": first.source}
        from .data import h4_offset
        ov = (info.get("basis_info") or {}).get("spot_overlay")
        grid = "18:00 New York (spot metals session open)" if h4_offset(sym) == 2 else "17:00 New York"
        feed = str(info.get("source"))
        data_note = {
            "feed": feed, "feed_short": feed.split(" (")[0].split(";")[0][:60] + (
                f" + Dukascopy spot candles on {ov['days']} day(s)" if ov else ""),
            "h4_grid": f"H4 candles start on the {grid} grid",
            "levels_caveat": "levels can differ from your broker's chart by several dollars/pips (different feed, bid "
                             "vs mid, futures-derived candles); compare the shape and which side of a line a candle "
                             "closed, not the exact price",
        }
        if info.get("basis_adjusted"):
            data_note["gold_basis"] = (
                (f"real spot candles (Dukascopy bid) on {ov['days']} cached day(s), {ov['first']} … {ov['last']}; "
                 if ov else "no real spot candles cached; ")
                + "other days are COMEX futures minus a basis interpolated between those days (can be several dollars "
                  "off intraday)")
        out = {
            "symbol": sym.id, "params": {"intervals": order, "pip": pip_used, "sl_pips": sl_pips, "tp_pips": tp_pips,
                                         "tp2_pips": tp2_pips, "fakeout_max": fakeout_max},
            **{k: v for k, v in info.items() if k not in ("basis_info", "history")},
            "as_of": iso(ctxs[order[-1]].closes[-1]), "now_utc": iso(now),
            "last_close": float(ctxs[order[-1]].df["c"].iloc[-1]),
            "data_note": data_note, **res, "odds": odds, "needs": needs,
            "notes": [
                f"{sym.id} pip={pip_used:g}. Bodies/closes define structure, wicks are sweeps. Box = highest of the last "
                f"two swing-high bodies / lowest of the last two swing-low bodies while swings are not trending; a "
                f"close must clear an edge by {BREAK_TOL_ATR} ATR to count as a break.",
                "Fixed-pip stops and targets are the caller's parameters; `stop_note` says when the stop sits inside "
                "earlier wicks. Odds are historical frequencies with a shuffled-candle baseline — 'no edge' means the "
                "rule did no better than random candles. Not financial advice.",
            ],
        }
        if info.get("basis_adjusted"):
            ov = (info.get("basis_info") or {}).get("spot_overlay")
            out["notes"].append(
                "Gold/silver: H4 bars start with the 18:00 New York session open. "
                + (f"Real spot candles (Dukascopy bid) are used on {ov['days']} cached day(s) ({ov['first']} … "
                   f"{ov['last']}); other days are COMEX futures minus a time-varying basis and can be a few dollars "
                   f"off a broker's spot chart." if ov else
                   "Candles are COMEX futures minus the futures-spot basis and can be a few dollars off a broker's "
                   "spot chart."))
        out["markdown"] = mtf_markdown(out)
        return out
    key = ("mtf", sym.id, tuple(ivs), pip_used, sl_pips, tp_pips, tp2_pips, fakeout_max, rk)
    return cache.get_or_set(key, TTL_INTRADAY, load)


def _pct(x) -> str:
    return "n/a" if x is None else f"{x * 100:.0f}%"


def mtf_markdown(r: dict) -> str:
    pip = r["params"]["pip"]
    st, pb = r["stack"], r["playbook"]
    dn = r.get("data_note") or {}
    L = [f"## {r['symbol']} top-down read ({' → '.join(r['params']['intervals'])}) — as of {r['now_utc']}",
         f"_Last close {_px(r['last_close'])} ({r['as_of']}); pip {pip:g}. Bodies define structure, wicks are sweeps._",
         f"_Data: {dn.get('feed_short', '')}. {dn.get('h4_grid', '')}. Levels can differ from your broker by several "
         f"dollars/pips — compare shapes, not exact prices._", ""]
    seen = set()
    for role in ("context", "setup", "trigger"):
        iv = st[role]["interval"]
        if iv in seen:
            continue
        seen.add(iv)
        tf = r["timeframes"][iv]
        box = tf.get("box")
        line = f"**{role.capitalize()} {iv} — {tf['regime'].replace('_', ' ')}.** "
        imp = (tf.get("lead_in_impulse") if box else None) or tf.get("last_impulse")
        if imp:
            line += (f"{'Lead-in' if box and tf.get('lead_in_impulse') else 'Last'} impulse {imp['direction']} "
                     f"{_px(imp['from'])}→{_px(imp['to'])} ({imp['pips']:.0f}p, {imp['candles']}c, ended "
                     f"{_t(imp['end'])}). ")
        if box:
            p = box["position"]
            line += (f"Box {_px(box['bottom'])}–{_px(box['top'])} (mid {_px(box['mid'])}, {box['height_pips']:.0f}p, "
                     f"wicks {_px(box['wick_low'])}/{_px(box['wick_high'])}, fakeouts ↑{box['fakeouts_above']} "
                     f"↓{box['fakeouts_below']}); ")
            line += (f"price inside at {p['pct_of_height']:.0f}%." if p["state"] == "inside" else
                     f"body CLOSED {'below' if p['state'] == 'closed_below' else 'above'} {_px(p['edge'])} "
                     f"{p['candles_ago']}c ago ({p['pips_beyond']:.0f}p beyond).")
        else:
            line += "Swings: " + " ".join(f"{s['label']} {_px(s['price'])}" for s in tf["swings"][-4:]) + "."
        L.append(line)
        if role != "trigger":
            for s in sorted(tf["sweeps"], key=lambda x: -x["pips_beyond"])[:2]:
                L.append(f"- sweep {s['side']} of {_px(s['level'])}: wick {_px(s['wick_extreme'])} "
                         f"(+{s['pips_beyond']:.0f}p) {_t(s['time'])}, closed {_px(s['close'])}")
            for f in tf["fakeouts"][-2:]:
                L.append(f"- fakeout {f['side']} {_px(f['edge'])}: body closed {_px(f['close'])} "
                         f"({f['close_beyond_pips']:.0f}p beyond) {_t(f['close_time'])}, back inside after "
                         f"{f['candles_until_back_inside']}c → {f['what_followed']}")
            if box:
                L.append(f"- {box['facts'][0]}; {box['facts'][1]}")
            for hb in tf["history"][-2:]:
                L.append(f"- earlier box {_px(hb['bottom'])}–{_px(hb['top'])} (first drawn {_px(hb['first_bottom'])}–"
                         f"{_px(hb['first_top'])}; {hb['start'][5:10]}→{hb['end'][5:10]}, "
                         f"lead-in {hb['lead_in_impulse']}): fakeouts ↑{hb['fakeouts_above']} ↓{hb['fakeouts_below']}, "
                         f"broke {hb['resolved']}"
                         + (" after a fakeout of the other side" if hb["preceded_by_opposite_fakeout"] else ""))
    if r.get("prior_box_edges"):
        L.append("- prior box edges nearby: " + "; ".join(
            f"{_px(e['level'])} ({e['interval']} {e['edge']}, {e['role']}, {e['distance_pips']:+.0f}p)"
            for e in r["prior_box_edges"][:4]))
    L += ["", f"**Reading:** {st['reading']}", "", f"**Playbook — {pb['mode'].replace('_', ' ')}**"
          + (f" ({pb['timeframe']} box)" if pb.get("timeframe") else "")]
    for i, cse in enumerate(pb.get("cases", []), 1):
        line = f"{i}. **{cse['action'].upper()}** when {cse['when']}"
        if cse["action"] != "hold":
            line += (f" → entry {cse['entry']}; stop {_px(cse['stop'])}; targets "
                     + ", ".join(f"{_px(t['price'])} ({t['name'].replace('_', ' ')})" for t in cse["targets"][:4]))
        line += f". _{cse['reason']}_"
        if cse.get("risk"):
            line += f" Risk: {cse['risk']}."
        L.append(line)
    L += ["", "**Reasons**"] + [f"{i}. {x}" for i, x in enumerate(pb["reasons"][:12], 1)]
    if r["odds"]:
        L += ["", "**Measured odds** (rate, 95% CI, n | shuffled-candle baseline → verdict)"]
        for iv, o in r["odds"].items():
            L.append(f"- {iv} ({o['candles']} candles since {o['period_start'][:10]}, {o['boxes_completed']} boxes): "
                     + "; ".join(
                         f"{k.replace('_', ' ')} {_pct(e['rate'])} ({_pct(e['ci95'][0])}–{_pct(e['ci95'][1])}, n={e['n']}"
                         f" | {_pct(e.get('baseline_rate'))} → {e['verdict'].split(' (')[0]})"
                         for k, e in o["events"].items() if e["n"]))
            rd = o["retest_depth_pips"]
            if rd["n"]:
                L.append(f"  - retest wick past the broken edge: median {rd['median']:.0f}p, p80 {rd['p80']:.0f}p "
                         f"(n={rd['n']})")
    if r["needs"]:
        L.append("\nNeeds: " + "; ".join(f"{n['interval']}: {n['reason']}" for n in r["needs"]))
    return "\n".join(L)


# ------------------------------------------------------------------ signals

def range_signals(ctx: Context, pip: float) -> list[dict]:
    """`range_sweep` / `range_edge` fired by the LAST closed candle (box known before that candle)."""
    df = ctx.df
    n = len(df)
    if n < 3:
        return []
    st = box_states(ctx)
    t = n - 1
    if st.status[t - 1] != INSIDE:
        return []
    h, l, c = (df[x].to_numpy(float) for x in ("h", "l", "c"))
    top, bot = float(st.top[t - 1]), float(st.bot[t - 1])
    a = ctx.atr[t - 1]
    if not np.isfinite(a):
        return []
    tol, zw = BREAK_TOL_ATR * a, zone_width(top - bot, a)
    out = []
    for side, edge, far, name in ((1, top, h, "high"), (-1, bot, l, "low")):
        box = {"top": top, "bottom": bot, "mid": (top + bot) / 2}
        if side * (far[t] - edge) > tol and side * (c[t] - edge) <= tol:
            out.append({"type": "range_sweep", "direction": "down" if side > 0 else "up", "side": name, "level": edge,
                        "wick_extreme": float(far[t]), "close": float(c[t]),
                        "pips_beyond": abs(far[t] - edge) / pip, "box": box})
        elif side * (far[t] - (edge - side * zw)) >= 0 and side * (far[t - 1] - (edge - side * zw)) < 0 \
                and side * (c[t] - edge) <= tol:
            out.append({"type": "range_edge", "direction": "down" if side > 0 else "up", "side": name, "level": edge,
                        "zone": {"low": edge - zw if side > 0 else edge, "high": edge if side > 0 else edge + zw},
                        "close": float(c[t]), "box": box})
    return out
