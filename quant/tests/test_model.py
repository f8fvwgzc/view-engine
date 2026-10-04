import numpy as np
import pandas as pd

from app import model


def test_walk_forward_splits_expanding_and_purged():
    sp = model.walk_forward_splits(1000, n_folds=5, min_train_frac=0.5, gap=3)
    assert len(sp) == 5
    prev_end = 0
    for tr_end, ts, te in sp:
        assert tr_end == ts - 3          # purge gap
        assert ts < te and te <= 1000
        assert tr_end > prev_end          # expanding
        prev_end = tr_end
    assert sp[0][1] == 500 and sp[-1][2] == 1000
    # test blocks are contiguous and non-overlapping
    for a, b in zip(sp, sp[1:]):
        assert a[2] == b[1]
    assert model.walk_forward_splits(6, n_folds=5) == []


def test_parse_horizon():
    assert model.parse_horizon("1", "1d") == ("1d", 1)
    assert model.parse_horizon(3, "1h") == ("1h", 3)
    assert model.parse_horizon("1d", None) == ("1d", 1)
    assert model.parse_horizon("4h", None) == ("1h", 4)
    assert model.parse_horizon("8h", "4h") == ("4h", 2)
    assert model.parse_horizon("1w", None) == ("1d", 5)


def test_features_no_lookahead():
    idx = pd.date_range("2020-01-01", periods=300, freq="B", tz="UTC")
    rng = np.random.default_rng(0)
    c = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, 300))), index=idx)
    df = pd.DataFrame({"o": c, "h": c * 1.001, "l": c * 0.999, "c": c, "v": 0.0})
    rel = {"X": c * 2}
    f1 = model.build_features(df, rel, intraday=False)
    # perturb the future: features up to t must not change
    df2 = df.copy()
    df2.iloc[200:, :4] *= 1.5
    f2 = model.build_features(df2, {"X": df2["c"] * 2}, intraday=False)
    pd.testing.assert_frame_equal(f1.iloc[:200], f2.iloc[:200])
    assert "X_ret1_lag1" in f1


def test_validate_on_synthetic_signal():
    rng = np.random.default_rng(0)
    n = 1200
    X = pd.DataFrame({"a": rng.normal(size=n), "b": rng.normal(size=n)})
    y = (X["a"] + 0.3 * rng.normal(size=n) > 0).astype(int).values
    v = model.validate(X, y, h=1)
    assert v["n_folds"] == 5 and v["n_oos"] > 500
    assert v["accuracy"] > v["baseline_accuracy"] + 0.2
    assert v["brier"] < v["baseline_brier"]
