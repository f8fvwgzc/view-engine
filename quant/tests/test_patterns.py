from types import SimpleNamespace as S

import numpy as np
import pandas as pd
import pytest

from app import patterns as P
from app import structure as st

NAMES = [n for n, _ in P.CS_FEATURES]


def cs(rows, sup=None, res=None, atr=1.0):
    a = np.array(rows, float)
    n = len(a)
    out = P.candle_patterns(a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.full(n, atr),
                            None if sup is None else np.full(n, sup), None if res is None else np.full(n, res))
    return dict(zip(NAMES, out[-1]))


PAD = [(10, 10.2, 9.8, 10.1)] * 10   # quiet lead-in candles (o, h, l, c)


def test_engulfing():
    assert cs(PAD + [(10.4, 10.5, 9.9, 10.0), (9.95, 10.8, 9.9, 10.7)])["cs_engulfing"] == 1
    assert cs(PAD + [(10.0, 10.5, 9.9, 10.4), (10.45, 10.5, 9.6, 9.7)])["cs_engulfing"] == -1
    assert cs(PAD + [(10.4, 10.5, 9.9, 10.0), (10.1, 10.4, 10.0, 10.3)])["cs_engulfing"] == 0   # does not engulf


def test_pin_bar():
    assert cs(PAD + [(10.0, 10.05, 9.0, 9.95)])["cs_pin"] == 1            # long lower wick, body at the top
    assert cs(PAD + [(10.0, 11.0, 9.95, 10.05)])["cs_pin"] == -1          # shooting star
    assert cs(PAD + [(10.0, 10.05, 9.6, 9.95)])["cs_pin"] == 0            # same shape but range < 0.75 ATR


def test_inside_bar_and_break_and_outside():
    mother, inside = (10.0, 11.0, 9.0, 10.5), (10.4, 10.8, 9.5, 10.2)
    assert cs(PAD + [mother, inside])["cs_inside"] == 1
    assert cs(PAD + [mother, (10.4, 11.2, 9.5, 10.2)])["cs_inside"] == 0  # pokes above the mother
    assert cs(PAD + [mother, inside, (10.2, 11.4, 10.1, 11.3)])["cs_inside_break"] == 1
    assert cs(PAD + [mother, inside, (10.2, 10.3, 8.7, 8.8)])["cs_inside_break"] == -1
    assert cs(PAD + [mother, inside, (10.2, 11.4, 10.1, 10.9)])["cs_inside_break"] == 0   # wick only, close inside
    assert cs(PAD + [(10, 10.3, 9.8, 10.1), (10.1, 10.6, 9.5, 10.5)])["cs_outside"] == 1
    assert cs(PAD + [(10, 10.3, 9.8, 10.1), (10.1, 10.6, 9.9, 10.5)])["cs_outside"] == 0  # no lower low


def test_doji_at_zone():
    doji = (10.0, 10.4, 9.6, 10.02)
    assert cs(PAD + [doji], sup=0.3, res=5.0)["cs_doji_zone"] == 1
    assert cs(PAD + [doji], sup=5.0, res=0.2)["cs_doji_zone"] == -1
    assert cs(PAD + [doji], sup=3.0, res=3.0)["cs_doji_zone"] == 0        # a doji, but not at a zone
    assert cs(PAD + [(10.0, 10.4, 9.6, 10.3)], sup=0.3, res=5.0)["cs_doji_zone"] == 0   # at a zone, not a doji


def test_morning_evening_star():
    assert cs(PAD + [(11.0, 11.1, 9.9, 10.0), (9.95, 10.1, 9.8, 9.9), (9.95, 10.9, 9.9, 10.8)])["cs_star"] == 1
    assert cs(PAD + [(10.0, 11.1, 9.9, 11.0), (11.05, 11.2, 10.9, 11.1), (11.05, 11.1, 10.1, 10.2)])["cs_star"] == -1
    assert cs(PAD + [(11.0, 11.1, 9.9, 10.0), (9.95, 10.1, 9.8, 9.9), (9.95, 10.4, 9.9, 10.3)])["cs_star"] == 0  # < mid


def test_three_soldiers_and_crows():
    assert cs(PAD + [(10.0, 10.5, 9.9, 10.4), (10.2, 10.9, 10.1, 10.8), (10.6, 11.3, 10.5, 11.2)])["cs_three"] == 1
    assert cs(PAD + [(11.0, 11.1, 10.5, 10.6), (10.8, 10.9, 10.1, 10.2), (10.4, 10.5, 9.7, 9.8)])["cs_three"] == -1
    assert cs(PAD + [(10.0, 10.5, 9.9, 10.4), (10.2, 10.9, 10.1, 10.8), (11.0, 11.5, 10.9, 11.4)])["cs_three"] == 0  # gap open


def test_tweezers():
    assert cs(PAD + [(10.0, 10.1, 9.0, 9.2), (9.2, 10.0, 9.02, 9.9)])["cs_tweezer"] == 1
    assert cs(PAD + [(10.0, 11.0, 9.9, 10.8), (10.8, 10.98, 10.0, 10.1)])["cs_tweezer"] == -1
    assert cs(PAD + [(10.0, 10.1, 9.0, 9.2), (9.2, 10.0, 9.4, 9.9)])["cs_tweezer"] == 0   # lows do not match


def sw(*pts):
    """Alternating swings from (kind, idx, price)."""
    return [S(kind=k, idx=i, price=p) for k, i, p in pts]


def test_double_and_triple_top_bottom():
    dt = P.detect(sw(("L", 0, 95), ("H", 10, 100), ("L", 20, 97), ("H", 30, 100.3)), atr=1.0)
    assert dt["double"]["dir"] == -1 and dt["double"]["line"][0] == 97 and dt["double"]["invalid"] == (100.8, 1)
    assert "double" not in P.detect(sw(("L", 0, 95), ("H", 10, 100), ("L", 20, 97), ("H", 30, 101.0)), atr=1.0)  # not equal
    assert "double" not in P.detect(sw(("L", 0, 95), ("H", 10, 100), ("L", 20, 99), ("H", 30, 100.2)), atr=1.0)  # too shallow
    db = P.detect(sw(("H", 0, 105), ("L", 10, 100), ("H", 20, 103), ("L", 30, 99.8)), atr=1.0)
    assert db["double"]["dir"] == 1 and db["double"]["line"][0] == 103
    tt = P.detect(sw(("H", 0, 100), ("L", 10, 97), ("H", 20, 100.3), ("L", 30, 96.5), ("H", 40, 99.9)), atr=1.0)
    assert tt["triple"]["dir"] == -1 and tt["triple"]["line"][0] == 96.5
    assert "triple" not in P.detect(sw(("H", 0, 100), ("L", 10, 97), ("H", 20, 102), ("L", 30, 96.5), ("H", 40, 99.9)),
                                    atr=1.0)


def test_head_and_shoulders():
    hs = P.detect(sw(("H", 0, 100), ("L", 10, 97), ("H", 20, 103), ("L", 30, 98), ("H", 40, 100.4)), atr=1.0)["hs"]
    assert hs["dir"] == -1 and hs["line"] == (98.0, 30, 0.05) and hs["invalid"] == (103, 1)   # sloped neckline
    assert P._at(hs["line"], 50) == pytest.approx(99.0)
    inv = P.detect(sw(("L", 0, 100), ("H", 10, 103), ("L", 20, 97), ("H", 30, 102), ("L", 40, 99.6)), atr=1.0)["hs"]
    assert inv["dir"] == 1
    # near-miss: the "head" is not clearly above the shoulders
    assert "hs" not in P.detect(sw(("H", 0, 100), ("L", 10, 97), ("H", 20, 100.3), ("L", 30, 98), ("H", 40, 100.1)), 1.0)


def test_triangles_and_wedges():
    asc = P.detect(sw(("H", 0, 100), ("L", 10, 96), ("H", 20, 100.1), ("L", 30, 97.5)), atr=1.0)["triangle"]
    assert asc["kind"] == "ascending" and asc["dir"] == 1 and asc["line"][0] == 100.1
    desc = P.detect(sw(("L", 0, 96), ("H", 10, 100), ("L", 20, 96.1), ("H", 30, 98.5)), atr=1.0)["triangle"]
    assert desc["kind"] == "descending" and desc["dir"] == -1 and desc["line"][0] == 96
    sym = P.detect(sw(("H", 0, 100), ("L", 10, 96), ("H", 20, 99), ("L", 30, 97)), atr=1.0)["triangle"]
    assert sym["kind"] == "symmetrical" and sym["dir"] == 0 and sym["line"][2] < 0 < sym["line2"][2]
    rising = P.detect(sw(("H", 0, 100), ("L", 10, 96), ("H", 20, 101), ("L", 30, 98.5)), atr=1.0)
    assert rising["wedge"]["dir"] == -1 and "triangle" not in rising        # lows rise faster than highs
    falling = P.detect(sw(("L", 0, 96), ("H", 10, 100), ("L", 20, 95), ("H", 30, 97.5)), atr=1.0)
    assert falling["wedge"]["dir"] == 1
    chan = P.detect(sw(("H", 0, 100), ("L", 10, 96), ("H", 20, 102), ("L", 30, 98)), atr=1.0)   # parallel channel
    assert "wedge" not in chan and "triangle" not in chan


def test_flag_and_pennant():
    pole = (1, 90.0, 100.0, 10)                                              # impulse up 90 -> 100, ended at idx 10
    alt = sw(("L", 0, 90), ("H", 10, 100), ("L", 16, 97), ("H", 20, 99))
    f = P.detect(alt, 1.0, pole, t=22)
    assert f["flag"]["dir"] == 1 and f["flag"]["line"][0] == 99 and f["flag"]["invalid"] == (95.0, -1)
    deep = P.detect(sw(("L", 0, 90), ("H", 10, 100), ("L", 16, 94), ("H", 20, 99)), 1.0, pole, t=22)
    assert "flag" not in deep and "pennant" not in deep                      # retraced more than half the pole
    assert "flag" not in P.detect(alt, 1.0, pole, t=80)                       # too long after the impulse
    pen = P.detect(sw(("L", 0, 90), ("H", 10, 100), ("L", 14, 97), ("H", 18, 99), ("L", 22, 97.8)), 1.0, pole, t=24)
    assert pen["pennant"]["dir"] == 1 and pen["pennant"]["line"][2] < 0 and "flag" not in pen
    bear = P.detect(sw(("H", 0, 110), ("L", 10, 100), ("H", 16, 103), ("L", 20, 101)), 1.0, (-1, 110.0, 100.0, 10), 22)
    assert bear["flag"]["dir"] == -1 and bear["flag"]["line"][0] == 101


def from_closes(closes, wick=0.2):
    rows, prev = [], closes[0]
    for c in closes:
        rows.append((prev, max(prev, c) + wick, min(prev, c) - wick, c))
        prev = c
    a = np.array(rows, float)
    idx = pd.date_range("2025-07-14", periods=len(a), freq="h", tz="UTC")
    return pd.DataFrame({"o": a[:, 0], "h": a[:, 1], "l": a[:, 2], "c": a[:, 3], "v": 1.0}, index=idx)


def test_double_top_state_machine_is_causal():
    lead = [90 + 0.3 * i + (0.4 if i % 2 else 0) for i in range(20)]           # drift up, ATR ~ 1
    top1 = [97, 98.5, 100, 99, 98, 97, 96.2]                                   # first top 100, pullback to ~96
    top2 = [97, 98, 99, 100.2, 99.2, 98.2, 97.4, 96.6]                         # second top 100.2, falling
    brk = [95.2, 94.5, 94.0]                                                   # body close below the neckline
    closes = lead + top1 + top2 + brk
    df = from_closes(closes)
    ctx = st.context_from_frame(None, "1h", df, df.index + pd.Timedelta(hours=1))
    stt = P.chart_pattern_state(ctx)["double"]
    i_top2 = len(lead) + len(top1) + 3                                         # candle of the second top
    assert stt["active"][:i_top2 + 2].sum() == 0                               # unknown until that swing is confirmed
    first = int(np.flatnonzero(stt["active"])[0])
    assert first == i_top2 + 2 and stt["dir"][first] == -1 and stt["state"][first] == 1 and stt["age"][first] == 0
    assert stt["line"][first] == pytest.approx(96.2)                           # neckline = the swing low between
    i_brk = len(lead) + len(top1) + len(top2)
    assert stt["state"][i_brk - 1] == 1 and stt["state"][i_brk] == 2           # confirmed by the close through it
    # cutting the series before the break leaves the earlier state identical
    cut = df.iloc[:i_brk]
    stc = P.chart_pattern_state(st.context_from_frame(None, "1h", cut, cut.index + pd.Timedelta(hours=1)))["double"]
    for k in ("active", "dir", "state"):
        np.testing.assert_array_equal(stt[k][:i_brk], stc[k])
    blk = P.chart_block(P.chart_pattern_state(ctx), np.arange(len(df)), df["c"].to_numpy(float), np.ones(len(df)))
    names = [f"{f}_{k}" for f, _ in P.FAMILIES for k, _ in P.CP_FIELDS]
    row = dict(zip(names, blk[i_brk]))
    assert row["double_state"] == 2 and row["double_dist_atr"] == pytest.approx(95.2 - 96.2)   # beyond the line
    assert dict(zip(names, blk[first]))["double_dist_atr"] > 0                 # still to go while forming
    # an invalidated pattern disappears: price closes above the tops instead
    up = from_closes(lead + top1 + top2[:6] + [99.5, 101.5, 102.0])
    su = P.chart_pattern_state(st.context_from_frame(None, "1h", up, up.index + pd.Timedelta(hours=1)))["double"]
    assert su["active"][-1] == 0


def test_feature_names_and_docs():
    names = P.pattern_feature_names()
    assert len(names) == len(set(names)) == 2 * (9 + 7 * 5) and set(P.pattern_feature_docs()) == set(names)
    assert "tr_cs_engulfing" in names and "ht1_cp_hs_dist_atr" in names
