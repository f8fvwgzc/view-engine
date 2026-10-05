"""Classic candlestick and chart-pattern detectors (structure-v3 features).

Everything is causal: a candlestick pattern is known at the close of the candle that completes it; a chart
pattern exists from the candle on which its last swing is CONFIRMED (two candles after the swing prints) and
is `confirmed` only by a body close through its trigger line. Tolerances are in ATR14 of the pattern's own
timeframe:

  EQ_TOL 0.5 ATR      highs / lows count as "equal" (double / triple tops, shoulders use 1.0 ATR)
  MIN_DEPTH 1.5 ATR   minimum distance between the equal highs / lows and the neckline
  FLAT 0.3 ATR, SLOPED 0.5 ATR   a triangle / wedge side is flat below 0.3 ATR and rising / falling above 0.5 ATR
  BREAK_TOL 0.1 ATR   a close must clear the trigger line by this much
  a forming pattern is dropped after 120 candles, a confirmed one after 40 more
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .structure import Context

EQ_TOL, SHOULDER_TOL, HEAD_MIN, MIN_DEPTH = 0.5, 1.0, 0.5, 1.5
FLAT, SLOPED, BREAK_TOL = 0.3, 0.5, 0.1
MAX_AGE, CONFIRMED_KEEP = 120, 40
IMP_MIN_ATR, IMP_SPEED_ATR, FLAG_MAX_AGE, FLAG_MAX_RETRACE = 2.5, 0.5, 40, 0.5

CS_FEATURES = [
    ("cs_engulfing", "+1 bullish / -1 bearish engulfing: body >= 0.3 ATR that engulfs the previous opposite body"),
    ("cs_pin", "+1 hammer / -1 shooting star: rejection wick >= 2/3 of the range, body in the far third, range >= 0.75 ATR"),
    ("cs_inside", "1 if the candle's high-low lies inside the previous candle's (inside bar)"),
    ("cs_inside_break", "+1 / -1 when the candle after an inside bar closes above / below the mother candle"),
    ("cs_outside", "+1 / -1 outside bar (higher high and lower low than the previous candle), signed by its close"),
    ("cs_doji_zone", "+1 / -1 doji (body <= 10% of a range >= 0.5 ATR) within 0.5 ATR of support / resistance"),
    ("cs_star", "+1 morning / -1 evening star: big body, small body (<= 0.25 ATR), then a close past the first body's middle"),
    ("cs_three", "+1 three white soldiers / -1 three black crows: three bodies >= 0.3 ATR, each closing further and "
                 "opening inside the previous body"),
    ("cs_tweezer", "+1 tweezer bottom / -1 tweezer top: two candles with matching lows / highs (<= 0.1 ATR) at a "
                   "10-candle extreme, the second reversing"),
]
FAMILIES = [
    ("double", "double top (-1) / double bottom (+1): two equal swing highs / lows; trigger = the swing between them"),
    ("triple", "triple top (-1) / triple bottom (+1): three equal swing highs / lows; trigger = the lower / higher of "
               "the two swings between them"),
    ("hs", "head and shoulders (-1) / inverse (+1): head beyond both shoulders by >= 0.5 ATR, shoulders within "
           "1 ATR; trigger = the sloped neckline through the two swings between them"),
    ("triangle", "ascending (+1: flat highs, rising lows), descending (-1: flat lows, falling highs) or symmetrical "
                 "(0 until it breaks: falling highs and rising lows); trigger = the flat side / the nearer line"),
    ("wedge", "rising wedge (-1: highs and lows rise, lows faster) / falling wedge (+1); trigger = the line on the "
              "break side"),
    ("flag", "bull (+1) / bear (-1) flag: after an impulse leg, a retracement of at most half of it; trigger = the "
             "last swing against the retracement (or the pole end)"),
    ("pennant", "bull (+1) / bear (-1) pennant: after an impulse leg, converging swings (lower highs and higher "
                "lows); trigger = the sloped line on the impulse side"),
]
CP_FIELDS = [
    ("active", "1 while the pattern is alive"),
    ("dir", "direction the pattern implies (+1 up, -1 down, 0 undecided)"),
    ("state", "1 forming, 2 confirmed by a body close through the trigger line, 0 none"),
    ("age", "candles of this timeframe since the pattern completed (its last swing was confirmed)"),
    ("dist_atr", "distance from price to the trigger line in trigger-timeframe ATR: positive = still to go, "
                 "negative = beyond the line"),
]


def pattern_feature_names(roles=("tr", "ht1")) -> list[str]:
    out = []
    for r in roles:
        out += [f"{r}_{n}" for n, _ in CS_FEATURES]
        out += [f"{r}_cp_{f}_{k}" for f, _ in FAMILIES for k, _ in CP_FIELDS]
    return out


def pattern_feature_docs(roles=("tr", "ht1")) -> dict[str, str]:
    d = {}
    for r in roles:
        d.update({f"{r}_{n}": f"[{r}] {t}" for n, t in CS_FEATURES})
        d.update({f"{r}_cp_{f}_{k}": f"[{r}] {ft} — {kt}" for f, ft in FAMILIES for k, kt in CP_FIELDS})
    return d


# ------------------------------------------------------------------ candlestick patterns (vectorised)

def candle_patterns(o, h, l, c, atr, sup_dist: Optional[np.ndarray] = None,
                    res_dist: Optional[np.ndarray] = None) -> np.ndarray:
    """(n x 9) signed flags, one row per candle, known at that candle's close. `atr` = ATR of the previous
    candle (so the candle's own size does not move its own threshold). `sup_dist` / `res_dist` = distance to
    support / resistance in ATR units for the doji-at-a-zone test."""
    n = len(c)
    out = np.zeros((n, len(CS_FEATURES)))
    if n < 4:
        return out
    a = np.where(np.isfinite(atr) & (atr > 0), atr, np.nan)
    body = c - o
    ab = np.abs(body)
    rng = h - l
    up_w = h - np.maximum(o, c)
    lo_w = np.minimum(o, c) - l

    def sh(x, k=1):
        return np.concatenate([np.full(k, np.nan), x[:-k]])
    po, pc, ph, pl, pbody = sh(o), sh(c), sh(h), sh(l), sh(body)
    with np.errstate(invalid="ignore", divide="ignore"):
        # engulfing
        bull = (body > 0) & (pbody < 0) & (o <= pc) & (c >= po) & (ab >= 0.3 * a) & (ab > np.abs(pbody))
        bear = (body < 0) & (pbody > 0) & (o >= pc) & (c <= po) & (ab >= 0.3 * a) & (ab > np.abs(pbody))
        out[:, 0] = np.where(bull, 1.0, np.where(bear, -1.0, 0.0))
        # pin bar
        big = rng >= 0.75 * a
        hammer = big & (lo_w >= 2 / 3 * rng) & (np.minimum(o, c) >= l + 2 / 3 * rng)
        star = big & (up_w >= 2 / 3 * rng) & (np.maximum(o, c) <= l + 1 / 3 * rng)
        out[:, 1] = np.where(hammer, 1.0, np.where(star, -1.0, 0.0))
        # inside bar and its break
        inside = (h <= ph) & (l >= pl) & (rng > 0)
        out[:, 2] = inside
        pin_, mh, ml = sh(inside.astype(float)), sh(ph), sh(pl)  # previous candle was inside its mother (2 back)
        out[:, 3] = np.where((pin_ == 1) & (c > mh), 1.0, np.where((pin_ == 1) & (c < ml), -1.0, 0.0))
        # outside bar
        outside = (h > ph) & (l < pl)
        out[:, 4] = np.where(outside & (body > 0), 1.0, np.where(outside & (body < 0), -1.0, 0.0))
        # doji at a zone
        doji = (ab <= 0.1 * rng) & (rng >= 0.5 * a)
        if sup_dist is not None and res_dist is not None:
            at_sup = doji & (sup_dist <= 0.5) & ~(res_dist < sup_dist)
            at_res = doji & (res_dist <= 0.5) & ~at_sup
            out[:, 5] = np.where(at_sup, 1.0, np.where(at_res, -1.0, 0.0))
        # morning / evening star
        b2, o2, c2 = sh(body, 2), sh(o, 2), sh(c, 2)
        small = np.abs(pbody) <= 0.25 * a
        mid2 = (o2 + c2) / 2
        morning = (b2 < 0) & (np.abs(b2) >= 0.5 * a) & small & (body > 0) & (c > mid2)
        evening = (b2 > 0) & (np.abs(b2) >= 0.5 * a) & small & (body < 0) & (c < mid2)
        out[:, 6] = np.where(morning, 1.0, np.where(evening, -1.0, 0.0))
        # three soldiers / crows
        ok = ab >= 0.3 * a
        s1 = (body > 0) & ok
        soldiers = s1 & (sh(s1.astype(float)) == 1) & (sh(s1.astype(float), 2) == 1) \
            & (c > pc) & (pc > c2) & (o >= po) & (o <= pc) & (po >= o2) & (po <= c2)
        k1 = (body < 0) & ok
        crows = k1 & (sh(k1.astype(float)) == 1) & (sh(k1.astype(float), 2) == 1) \
            & (c < pc) & (pc < c2) & (o <= po) & (o >= pc) & (po <= o2) & (po >= c2)
        out[:, 7] = np.where(soldiers, 1.0, np.where(crows, -1.0, 0.0))
        # tweezers
        win = np.lib.stride_tricks.sliding_window_view
        lo10, hi10 = np.full(n, np.nan), np.full(n, np.nan)
        if n >= 10:
            lo10[9:], hi10[9:] = win(l, 10).min(axis=1), win(h, 10).max(axis=1)
        tb = (np.abs(l - pl) <= 0.1 * a) & (pbody < 0) & (body > 0) & (np.minimum(l, pl) <= lo10)
        tt = (np.abs(h - ph) <= 0.1 * a) & (pbody > 0) & (body < 0) & (np.maximum(h, ph) >= hi10)
        out[:, 8] = np.where(tb, 1.0, np.where(tt, -1.0, 0.0))
    out[~np.isfinite(out)] = 0.0
    return out


# ------------------------------------------------------------------ chart patterns from confirmed swings

def _line(p0: float, i0: int, slope: float = 0.0) -> tuple:
    return (float(p0), int(i0), float(slope))


def _at(line: tuple, t: int) -> float:
    return line[0] + line[2] * (t - line[1])


def _pts(swings: list) -> list:
    return [(int(x.idx), float(x.price)) for x in swings]


def detect(alt: list, atr: float, impulse: Optional[tuple] = None, t: Optional[int] = None) -> dict:
    """Patterns visible in the alternating confirmed body swings `alt` (oldest -> newest; objects with .kind
    'H'/'L', .idx, .price). `impulse` = (dir, start_price, end_price, end_idx) of the last impulse leg.
    Returns {family: spec}; spec = dir, line, optional line2 (symmetrical triangle), invalid (level, side),
    pts = the (idx, price) swing points that make up the shape (for drawing)."""
    out: dict = {}
    if not np.isfinite(atr) or atr <= 0 or len(alt) < 3:
        return out
    s1, s2, s3 = alt[-1], alt[-2], alt[-3]
    top = s1.kind == "H"
    sg = -1 if top else 1  # direction the reversal patterns imply
    eq = abs(s1.price - s3.price) <= EQ_TOL * atr
    depth = (min(s1.price, s3.price) - s2.price) if top else (s2.price - max(s1.price, s3.price))
    lead_ok = len(alt) < 4 or ((alt[-4].price < s2.price) if top else (alt[-4].price > s2.price))
    if eq and depth >= MIN_DEPTH * atr and lead_ok:
        ext = max(s1.price, s3.price) if top else min(s1.price, s3.price)
        out["double"] = {"dir": sg, "line": _line(s2.price, s2.idx), "invalid": (ext - sg * EQ_TOL * atr, -sg),
                         "key": s1.idx, "pts": _pts([s3, s2, s1])}
    if len(alt) >= 5:
        s4, s5 = alt[-4], alt[-5]
        ext3 = [s1.price, s3.price, s5.price]
        mids = [s2.price, s4.price]
        if max(ext3) - min(ext3) <= 1.5 * EQ_TOL * atr:
            neck = min(mids) if top else max(mids)
            d3 = (min(ext3) - neck) if top else (neck - max(ext3))
            if d3 >= MIN_DEPTH * atr:
                e = max(ext3) if top else min(ext3)
                out["triple"] = {"dir": sg, "line": _line(neck, s2.idx), "invalid": (e - sg * EQ_TOL * atr, -sg),
                                 "key": s1.idx, "pts": _pts([s5, s4, s3, s2, s1])}
        head_out = (s3.price - max(s1.price, s5.price)) if top else (min(s1.price, s5.price) - s3.price)
        if head_out >= HEAD_MIN * atr and abs(s1.price - s5.price) <= SHOULDER_TOL * atr:
            slope = (s2.price - s4.price) / max(s2.idx - s4.idx, 1)
            out["hs"] = {"dir": sg, "line": _line(s2.price, s2.idx, slope), "invalid": (s3.price, -sg), "key": s1.idx,
                         "pts": _pts([s5, s4, s3, s2, s1])}
    if len(alt) >= 4:
        hs = [x for x in alt[-4:] if x.kind == "H"]
        ls = [x for x in alt[-4:] if x.kind == "L"]
        if len(hs) == 2 and len(ls) == 2:
            dh, dl = hs[1].price - hs[0].price, ls[1].price - ls[0].price
            slh = dh / max(hs[1].idx - hs[0].idx, 1)
            sll = dl / max(ls[1].idx - ls[0].idx, 1)
            up_line, lo_line = _line(hs[1].price, hs[1].idx, slh), _line(ls[1].price, ls[1].idx, sll)
            flat_h, flat_l = abs(dh) <= FLAT * atr, abs(dl) <= FLAT * atr
            rise_h, fall_h = dh >= SLOPED * atr, dh <= -SLOPED * atr
            rise_l, fall_l = dl >= SLOPED * atr, dl <= -SLOPED * atr
            key = max(hs[1].idx, ls[1].idx)
            four = _pts(alt[-4:])
            if flat_h and rise_l:
                out["triangle"] = {"dir": 1, "line": _line(max(hs[0].price, hs[1].price), hs[1].idx),
                                   "invalid": (ls[1].price - EQ_TOL * atr, -1), "key": key, "kind": "ascending",
                                   "pts": four}
            elif flat_l and fall_h:
                out["triangle"] = {"dir": -1, "line": _line(min(ls[0].price, ls[1].price), ls[1].idx),
                                   "invalid": (hs[1].price + EQ_TOL * atr, 1), "key": key, "kind": "descending",
                                   "pts": four}
            elif fall_h and rise_l:
                out["triangle"] = {"dir": 0, "line": up_line, "line2": lo_line, "invalid": None, "key": key,
                                   "kind": "symmetrical", "pts": four}
            elif rise_h and rise_l and sll > slh:
                out["wedge"] = {"dir": -1, "line": lo_line, "invalid_line": (up_line, 1), "key": key, "pts": four}
            elif fall_h and fall_l and slh < sll:
                out["wedge"] = {"dir": 1, "line": up_line, "invalid_line": (lo_line, -1), "key": key, "pts": four}
    if impulse is not None and t is not None:
        d, p_start, p_end, e_idx = impulse[:4]
        pole = [(int(impulse[4]), float(p_start))] if len(impulse) > 4 else []
        pole.append((int(e_idx), float(p_end)))
        pole_len = abs(p_end - p_start)
        after = [x for x in alt if x.idx > e_idx]
        if d != 0 and pole_len > 0 and 0 < t - e_idx <= FLAG_MAX_AGE and after:
            against = [x for x in after if x.kind == ("L" if d > 0 else "H")]  # pullback swings
            with_ = [x for x in after if x.kind == ("H" if d > 0 else "L")]  # swings on the impulse side
            if against:
                worst = min(x.price for x in against) if d > 0 else max(x.price for x in against)
                retr = d * (p_end - worst) / pole_len
                beyond = any(d * (x.price - p_end) > EQ_TOL * atr for x in with_)
                if 0 < retr <= FLAG_MAX_RETRACE and not beyond:
                    inv = (p_end - d * FLAG_MAX_RETRACE * pole_len, -d)
                    conv = (len(against) >= 2 and with_ and d * (against[-1].price - against[0].price) >= FLAT * atr
                            and d * (p_end - with_[-1].price) >= FLAT * atr)
                    if conv:
                        w = with_[-1]
                        slope = (w.price - p_end) / max(w.idx - e_idx, 1)
                        out["pennant"] = {"dir": d, "line": _line(w.price, w.idx, slope), "invalid": inv,
                                          "key": after[-1].idx, "pts": pole + _pts(after)}
                    else:
                        lvl, li = (with_[-1].price, with_[-1].idx) if with_ else (p_end, e_idx)
                        out["flag"] = {"dir": d, "line": _line(lvl, li), "invalid": inv, "key": after[-1].idx,
                                       "pts": pole + _pts(after)}
    return out


def chart_pattern_state(ctx: Context, log: Optional[list] = None) -> dict[str, dict[str, np.ndarray]]:
    """Per family, per candle of ctx (state after that candle closed): active, dir, state, age, line.
    With `log` (a list), every pattern instance is appended to it as a dict: family, dir, start (candle on which
    it became known), state (1 forming / 2 confirmed), conf (confirmation candle), end (candle on which it was
    dropped or replaced; None = still alive), line / line2 / invalid / pts. Does not change the arrays."""
    n = len(ctx.df)
    c = ctx.df["c"].to_numpy(float)
    atr = ctx.atr
    hist = sorted(ctx.run.history, key=lambda s: (s.confirmed_idx, s.idx))
    fam = [f for f, _ in FAMILIES]
    out = {f: {"active": np.zeros(n), "dir": np.zeros(n), "state": np.zeros(n), "age": np.full(n, np.nan),
               "line": np.full(n, np.nan)} for f in fam}
    cur: dict = {}
    alt: list = []
    hp = 0
    impulse = None
    for t in range(n):
        a = atr[t]
        changed = False
        while hp < len(hist) and hist[hp].confirmed_idx <= t:
            s = hist[hp]
            hp += 1
            if alt and alt[-1].kind == s.kind:
                alt.pop()
            alt.append(s)
            changed = True
        if changed:
            impulse = None
            for x, y in zip(reversed(alt[-9:-1]), reversed(alt[-8:])):
                nb, av = y.idx - x.idx, atr[x.idx]
                mv = y.price - x.price
                if nb > 0 and np.isfinite(av) and av > 0 and abs(mv) >= IMP_MIN_ATR * av \
                        and abs(mv) / nb >= IMP_SPEED_ATR * av:
                    impulse = (1 if mv > 0 else -1, x.price, y.price, y.idx, x.idx)
                    break
            for f, spec in detect(alt, a, impulse, t).items():
                old = cur.get(f)
                if old is None or old["key"] != spec["key"]:
                    if old is not None:
                        old["end"] = t
                    cur[f] = {**spec, "family": f, "start": t, "state": 1, "conf": None, "end": None}
                    if log is not None:
                        log.append(cur[f])
        if not cur:
            continue
        tol = BREAK_TOL * a if np.isfinite(a) else 0.0
        for f in list(cur):
            p = cur[f]
            line = _at(p["line"], t)
            if p["state"] == 1:
                if p["dir"] != 0:
                    if p["dir"] * (c[t] - line) > tol and t > p["start"]:
                        p["state"], p["conf"] = 2, t
                else:
                    lo = _at(p["line2"], t)
                    if c[t] - line > tol:
                        p["dir"], p["state"], p["conf"] = 1, 2, t
                    elif lo - c[t] > tol:
                        p["dir"], p["state"], p["conf"], p["line"] = -1, 2, t, p["line2"]
                        line = lo
                    elif abs(lo - c[t]) < abs(line - c[t]):
                        line = lo
                dead = t - p["start"] > MAX_AGE
                if p["state"] == 1 and not dead:
                    inv = p.get("invalid")
                    if inv is not None and inv[1] * (c[t] - inv[0]) > 0:
                        dead = True
                    il = p.get("invalid_line")
                    if il is not None and il[1] * (c[t] - _at(il[0], t)) > tol:
                        dead = True
                if dead:
                    p["end"] = t
                    del cur[f]
                    continue
            elif t - p["conf"] > CONFIRMED_KEEP:
                p["end"] = t
                del cur[f]
                continue
            o_ = out[f]
            o_["active"][t], o_["dir"][t], o_["state"][t] = 1.0, p["dir"], p["state"]
            o_["age"][t], o_["line"][t] = t - p["start"], line
    return out


def chart_block(state: dict, pos: np.ndarray, close: np.ndarray, atr_tr: np.ndarray) -> np.ndarray:
    """Map a timeframe's chart-pattern state onto trigger rows; distance uses the trigger close / trigger ATR."""
    ok = pos >= 0
    p = np.where(ok, pos, 0)
    cols = []
    for f, _ in FAMILIES:
        s = state[f]
        act = np.where(ok, s["active"][p], 0.0)
        d = np.where(ok, s["dir"][p], 0.0)
        line = np.where(ok, s["line"][p], np.nan)
        with np.errstate(invalid="ignore", divide="ignore"):
            dist = np.where(d != 0, d * (line - close) / atr_tr, np.abs(line - close) / atr_tr)
        cols += [act, d, np.where(ok, s["state"][p], 0.0), np.where(act == 1, s["age"][p], np.nan),
                 np.where(act == 1, dist, np.nan)]
    return np.column_stack(cols)
