"""Training: build the rows, fit, calibrate, tune the policy on validation rows, report on the holdout.

Setup heads (time-ordered, pooled over instruments, one cut-off instant for all of them):

    |------------------ development (first 80% of time) ------------------|--- holdout (last 20%) ---|
    |---------- fit (75%) ----------|------ validation (25%) ------|
      model A: fit + calibrate         tune policy thresholds          model B (fit + calibrate on all of
                                                                       development) is scored here once;
                                                                       model B is the one that is served

Every boundary is purged: a row is dropped from the earlier side when its label window (the next `horizon`
candles of its own instrument) reaches into the later side. The holdout is never used for fitting, calibrating
or tuning.

Intent heads: seed rows split by template (20% of templates held out), feedback rows added to training only.
"""
from __future__ import annotations

import json
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import numpy as np

from . import bernoulli as B
from . import metrics as M
from . import policy as P
from .heads import base, intent, setup
from .quant import QuantClient, QuantRejected, QuantUnavailable, check_override
from .seeds import intent as seeds
from .store import Store, now_iso

DEFAULT_PIPS = {"XAUUSD": [0.1, 1.0]}
HOLDOUT_FRACTION = 0.2
VALIDATION_FRACTION = 0.25  # of development
MIN_SLICE_HOLDOUT = 50


class TrainError(Exception):
    pass


def parse_time(s: str) -> int:
    return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())


def segment_key(symbol: str, pip: Any) -> str:
    return f"{symbol}@{float(pip):g}"


# ----------------------------------------------------------------------------- setup rows

@dataclass
class SetupData:
    interval: str
    feature_version: str
    feature_names: list[str]
    timeframes: dict
    segments: list[dict]            # one per (symbol, pip): key, symbol, pip, spread_pips, n, start, end
    X: np.ndarray                   # float32, NaN = unknown
    y: np.ndarray                   # 0 buy, 1 sell, 2 hold
    t: np.ndarray                   # candle close, epoch seconds
    t_end: np.ndarray               # close of the last candle of the label window
    seg: np.ndarray                 # index into segments
    r_buy: np.ndarray               # realised R of a long from this close (net of spread)
    r_sell: np.ndarray
    stride: int = 1                 # every stride-th labelled candle of each segment was kept
    rows_available: int = 0
    notes: list[str] = field(default_factory=list)
    labels: Optional[np.ndarray] = None      # (n, L) float32 extra yes/no outcome labels, NaN = not applicable
    label_names: list[str] = field(default_factory=list)
    label_docs: dict = field(default_factory=dict)
    feature_groups: Optional[dict] = None    # feature -> session | news | structure | zones | ...


class Source:
    """One sidecar export (a symbol at one pip size), from either wire format. The feature matrix is only
    materialised in `rows`, already thinned, so a multi-year 5m export never sits in memory twice."""

    def __init__(self, meta: dict, label: np.ndarray, times: np.ndarray, load: Callable[[str], np.ndarray]):
        self.meta, self.times, self._load = meta, np.asarray(times, np.int64), load
        self.label = np.asarray(label, np.int8)   # RLCD order: 0 buy, 1 sell, 2 hold, -1 unlabelled
        self.labelled = int((self.label >= 0).sum())

    @classmethod
    def from_json(cls, d: dict) -> "Source":
        label = np.array([setup.LABEL.get(b, -1) for b in (d.get("y") or {}).get("best") or []], np.int8)
        times = np.array([parse_time(x) for x in d.get("times") or []], np.int64)
        n = len(times)

        def load(name: str) -> np.ndarray:
            if name == "X":
                return np.array(d["X"], dtype=np.float32).reshape(n, -1)
            raw = d["y"].get(name)
            return (np.full(n, np.nan, np.float32) if raw is None
                    else np.array([np.nan if v is None else v for v in raw], np.float32))
        return cls({k: v for k, v in d.items() if k not in ("X", "y", "times")}, label, times, load)

    @classmethod
    def from_npz(cls, path: Any) -> "Source":
        z = np.load(path, allow_pickle=False)   # members are read on access; a download is never unpickled
        meta = json.loads(str(z["meta"][()]))
        wire = z["best"].astype(np.int8)        # wire order: 0 hold, 1 buy, 2 sell, -1 unlabelled
        label = np.select([wire == 1, wire == 2, wire == 0], [P.BUY, P.SELL, P.HOLD], -1).astype(np.int8)

        def load(name: str) -> np.ndarray:
            return z[name] if name in z.files else np.full(len(label), np.nan, np.float32)
        return cls(meta, label, z["times"], load)

    def rows(self, interval: str, horizon: int, stride: int, label_names: tuple = ()) -> Optional[dict]:
        t, n, step = self.times, len(self.times), setup.INTERVAL_SECONDS[interval]
        if n == 0 or len(self.label) != n or not self.labelled:
            return None
        keep = np.flatnonzero(self.label >= 0)[::stride]
        # label window of row i = the next `horizon` candles of this export; past its end, extrapolate
        idx = keep + horizon
        t_end = t[np.minimum(idx, n - 1)] + np.maximum(0, idx - (n - 1)) * step
        y = self.label[keep].astype(np.int64)
        sl, tp = float(self.meta["params"]["sl_pips"]), float(self.meta["params"]["tp_pips"])
        cost_r = float(self.meta["params"].get("spread_pips") or 0.0) / sl

        def realised(name: str, cls_: int) -> np.ndarray:
            r = np.asarray(self._load(name), np.float32)[keep]
            # fallback when the sidecar gave no R: a win pays tp/sl, anything else counts as a full stop-out
            return np.where(np.isnan(r), np.where(y == cls_, tp / sl, -1.0) - cost_r, r).astype(np.float32)

        X = np.ascontiguousarray(np.asarray(self._load("X"), np.float32)[keep])
        labels = np.full((len(keep), len(label_names)), np.nan, np.float32)
        for j, name in enumerate(label_names):
            labels[:, j] = np.asarray(self._load(name), np.float32)[keep]
        return {"X": X, "y": y, "t": t[keep], "t_end": t_end, "r_buy": realised("long_r", P.BUY),
                "r_sell": realised("short_r", P.SELL), "labels": labels}


def fetch_source(quant: QuantClient, store: Store, symbol: str, interval: str, pip: Optional[float],
                 req: Any) -> Source:
    kw = dict(sl_pips=req.sl_pips, tp_pips=req.tp_pips, tp2_pips=req.tp2_pips, horizon=req.horizon, pip=pip,
              start=req.start, end=req.end)
    if req.format in ("auto", "npz"):
        name = (f"{symbol}_{interval}_{'default' if pip is None else format(pip, 'g')}_sl{req.sl_pips:g}"
                f"_tp{req.tp_pips:g}_h{req.horizon}.npz")
        path = quant.dataset_npz(store.home / "datasets" / name, symbol, interval, **kw)
        if path is not None:
            return Source.from_npz(path)
        if req.format == "npz":
            raise TrainError("the quant sidecar does not offer format=npz; use format=json or auto")
    return Source.from_json(quant.dataset(symbol, interval, max_rows=req.max_rows, **kw))


def load_setup_data(quant: QuantClient, store: Store, interval: str, symbols: list[str], pips: dict, req: Any,
                    progress: Callable[[str], None] = lambda m: None) -> SetupData:
    sources, notes = [], []
    version = names = roles = None
    for symbol in symbols:
        for pip in (pips.get(symbol) or [None]):
            progress(f"{interval}: exporting {symbol}" + (f" pip={pip:g}" if pip else ""))
            try:
                src = fetch_source(quant, store, symbol, interval, pip, req)
            except QuantRejected as e:
                notes.append(f"skipped {symbol}: {e}")
                continue
            m = src.meta
            if version is None:
                version, names, roles = m["feature_version"], list(m["feature_names"]), m.get("timeframes") or {}
            elif m["feature_version"] != version or list(m["feature_names"]) != names:
                notes.append(f"skipped {symbol}: feature_version {m['feature_version']} differs from {version}")
                continue
            if not src.labelled:
                notes.append(f"skipped {symbol}: no labelled rows")
                continue
            sources.append(src)
    if not sources:
        raise TrainError(f"no training rows for setup:{interval}: " + ("; ".join(notes) or "no symbols"))
    # Thin to the row budget by taking every k-th candle of each symbol: neighbouring candles are near
    # duplicates (their label windows overlap almost entirely), so this loses little and keeps time order.
    total = sum(s.labelled for s in sources)
    stride = max(1, -(-total // int(req.max_train_rows)))
    # extra outcome labels are discovered from the export, so a label the sidecar adds later trains by itself
    meta0 = sources[0].meta
    label_names = tuple(n for n in (meta0.get("label_names") or []) if n not in base.DERIVED_LABELS)
    parts, segments = [], []
    for src in sources:
        m = src.meta
        progress(f"{interval}: loading {m['symbol']} pip={float(m['pip']):g}, every {stride} candle")
        rows = src.rows(interval, int(req.horizon), stride, label_names)
        if rows is None:
            continue
        rows["seg"] = np.full(len(rows["y"]), len(segments), np.int32)
        segments.append({"key": segment_key(m["symbol"], m["pip"]), "symbol": m["symbol"],
                         "pip": float(m["pip"]), "spread_pips": float(m["params"].get("spread_pips") or 0.0),
                         "n": int(len(rows["y"])), "rows_available": src.labelled,
                         "start": m.get("start"), "end": m.get("end"), "source": m.get("source")})
        parts.append(rows)

    def cat(k):
        return np.concatenate([p[k] for p in parts])
    order = np.lexsort((cat("seg"), cat("t")))
    if stride > 1:
        notes.append(f"{total} labelled rows available; kept every {stride} candle per symbol "
                     f"({len(order)} rows) to stay within max_train_rows={req.max_train_rows}")
    return SetupData(interval, version, names, roles, segments, cat("X")[order], cat("y")[order], cat("t")[order],
                     cat("t_end")[order], cat("seg")[order], cat("r_buy")[order], cat("r_sell")[order],
                     stride, total, notes, cat("labels")[order], list(label_names),
                     dict(meta0.get("label_docs") or {}), meta0.get("feature_groups"))


def split_by_time(t: np.ndarray, t_end: np.ndarray, later_fraction: float) -> tuple[np.ndarray, np.ndarray, int]:
    """(earlier mask, later mask, cut). One cut-off instant for every symbol, placed so that the later side is
    the last `later_fraction` of the TIME spanned (not of the row count). `later` = rows from the cut on;
    `earlier` = rows before it whose label window had closed by the cut (the purge)."""
    lo, hi = int(t.min()), int(t.max())
    cut = int(lo + (hi - lo) * (1 - later_fraction))
    return (t < cut) & (t_end <= cut), t >= cut, cut


def feedback_rows(store: Store, head: str, data: SetupData, horizon: int) -> tuple[dict, list[str]]:
    """Scored outcomes posted to /v1/feedback for this head, as extra rows (same feature version only)."""
    seg_index = {s["key"]: i for i, s in enumerate(data.segments)}
    step, f = setup.INTERVAL_SECONDS[data.interval], len(data.feature_names)
    X, y, w, t, seg, skipped = [], [], [], [], [], 0
    for r in store.feedback(head):
        x, label = r.get("x"), r.get("label")
        if (r.get("feature_version") != data.feature_version or not isinstance(x, list) or len(x) != f
                or label not in setup.LABEL):
            skipped += 1
            continue
        X.append([np.nan if v is None else float(v) for v in x])
        y.append(setup.LABEL[label])
        w.append(float(r.get("weight") or 1.0))
        try:
            t.append(parse_time(r["time"]))
        except Exception:
            t.append(-1)
        key = segment_key(r["symbol"], r["pip"]) if r.get("symbol") and r.get("pip") else None
        seg.append(seg_index.get(key, -1))
    notes = [f"{skipped} feedback rows skipped (other feature_version, wrong length or label)"] if skipped else []
    t = np.array(t, np.int64)
    return {"X": np.array(X, np.float32).reshape(len(y), f), "y": np.array(y, np.int64),
            "w": np.array(w, float), "t": t, "t_end": t + horizon * step, "seg": np.array(seg, np.int32)}, notes


def directional_auc(Pm: np.ndarray, y: np.ndarray) -> Optional[float]:
    """Among rows where one side did win: does P(buy) - P(sell) rank the buys above the sells? 0.5 = no
    directional information (a model can beat the base rate on volatility alone and still score 0.5 here)."""
    m = y != P.HOLD
    if m.sum() < 20 or len(np.unique(y[m])) < 2:
        return None
    from sklearn.metrics import roc_auc_score
    return M._f(roc_auc_score((y[m] == P.BUY).astype(int), Pm[m, P.BUY] - Pm[m, P.SELL]))


def train_setup(head: str, data: SetupData, store: Store, req: Any,
                progress: Callable[[str], None] = lambda m: None) -> dict:
    """Fit, calibrate, tune and report one setup head. Returns the artifact to store."""
    H, sl, tp = int(req.horizon), float(req.sl_pips), float(req.tp_pips)
    n = len(data.y)
    if n < 600:
        raise TrainError(f"{head}: only {n} labelled rows; need at least 600")
    spread = np.array([s["spread_pips"] for s in data.segments])
    dev, hold, cut = split_by_time(data.t, data.t_end, HOLDOUT_FRACTION)
    fb, notes = feedback_rows(store, head, data, H)
    n_fb = int(len(fb["y"]))
    if n_fb:
        # A scored outcome inside the holdout window is used for training, so the holdout rows of the same
        # instrument whose label windows overlap it are removed: the holdout stays untouched by training.
        for i in range(n_fb):
            if fb["seg"][i] >= 0 and fb["t"][i] >= 0:
                rows = np.flatnonzero(hold & (data.seg == fb["seg"][i]))
                pos = int(np.searchsorted(data.t[rows], fb["t"][i]))
                hold[rows[max(0, pos - H):pos + H + 1]] = False
        fb["t"] = np.where(fb["t"] < 0, cut - 1, fb["t"])
        fb["t_end"] = np.where(fb["t_end"] < fb["t"], fb["t"], fb["t_end"])

    # development rows = market rows before the holdout + feedback rows, in time order
    d_idx = np.flatnonzero(dev)
    Xd = np.vstack([data.X[d_idx], fb["X"]]) if n_fb else data.X[d_idx]
    yd = np.concatenate([data.y[d_idx], fb["y"]])
    wd = np.concatenate([np.ones(len(d_idx)), fb["w"] * float(req.feedback_weight)])
    td = np.concatenate([data.t[d_idx], fb["t"]])
    ted = np.concatenate([data.t_end[d_idx], fb["t_end"]])
    segd = np.concatenate([data.seg[d_idx], fb["seg"]])
    is_fb = np.concatenate([np.zeros(len(d_idx), bool), np.ones(n_fb, bool)])
    rbd = np.concatenate([data.r_buy[d_idx], np.full(n_fb, np.nan, np.float32)])
    rsd = np.concatenate([data.r_sell[d_idx], np.full(n_fb, np.nan, np.float32)])
    if n_fb:
        o = np.argsort(td, kind="stable")
        Xd, yd, wd, td, ted, segd, is_fb, rbd, rsd = (a[o] for a in (Xd, yd, wd, td, ted, segd, is_fb, rbd, rsd))
    n_hold = int(hold.sum())
    if len(yd) < 400 or n_hold < 100:
        raise TrainError(f"{head}: {len(yd)} development and {n_hold} holdout rows after the purge; too few")
    if len(np.unique(yd)) < 3:
        raise TrainError(f"{head}: development rows do not contain all three outcomes")

    kw = dict(horizon=H, n_estimators=int(req.n_estimators), seed=int(req.seed))
    cfg = P.PolicyConfig()
    # --- model A on the fit part, thresholds tuned on the validation part
    progress(f"{head}: fitting on {len(yd)} rows, tuning thresholds on validation")
    market = ~is_fb
    fit_m, val_m, _ = split_by_time(td[market], ted[market], VALIDATION_FRACTION)
    fit_m, val_m = _expand(fit_m, market), _expand(val_m, market)
    try:
        model_a, _ = setup.fit(Xd[fit_m], yd[fit_m], wd[fit_m], td[fit_m], ted[fit_m], **kw)
        Pv = model_a.predict_proba(Xd[val_m])
        cfg, tuning = P.tune(Pv, yd[val_m], rbd[val_m], rsd[val_m], segd[val_m], td[val_m], ted[val_m], sl, tp,
                             spread[segd[val_m]], cfg)
    except setup.NotEnoughData as e:
        tuning = {"tuned": False, "reason": f"validation fit not possible ({e}); defaults kept"}

    # --- model B on all of development: the served model, scored once on the holdout
    progress(f"{head}: refitting on development, scoring the holdout ({n_hold} rows)")
    try:
        model, calib = setup.fit(Xd, yd, wd, td, ted, **kw)
    except setup.NotEnoughData as e:
        raise TrainError(f"{head}: {e}")
    h_idx = np.flatnonzero(hold)
    Xh, yh, th, teh, segh = data.X[h_idx], data.y[h_idx], data.t[h_idx], data.t_end[h_idx], data.seg[h_idx]
    rbh, rsh = data.r_buy[h_idx], data.r_sell[h_idx]
    Ph = model.predict_proba(Xh)

    # baseline: each row's own instrument's class frequencies in development (market rows only)
    k = len(setup.CLASSES)
    pooled = np.bincount(yd[market], minlength=k) / market.sum()
    seg_prior = np.tile(pooled, (len(data.segments), 1))
    seg_train = np.zeros(len(data.segments), int)
    for s in range(len(data.segments)):
        m = market & (segd == s)
        seg_train[s] = int(m.sum())
        if m.sum() >= 50:
            seg_prior[s] = np.bincount(yd[m], minlength=k) / m.sum()
    prior = seg_prior[segh]
    note = "predicts, for each row, the class frequencies of that row's own instrument and pip in training"

    bcfg = cfg.bernoulli()

    def sliced(m: np.ndarray) -> dict:
        """Holdout report of one slice (an instrument, a calendar year), with its own policy result."""
        rep = M.calibration_report(Ph[m], yh[m], setup.CLASSES, prior[m], note)
        rep.pop("definitions")
        rep["directional_auc"] = directional_auc(Ph[m], yh[m])
        pol = P.evaluate(Ph[m], yh[m], rbh[m], rsh[m], segh[m], th[m], teh[m], sl, tp, spread[segh[m]], cfg)
        rep["policy"] = {"holdout": pol, "verdict": P.verify(rep["skill"]["has_skill"], pol, cfg, pooled=False),
                         "bernoulli": {"act": B.evidence(pol["act"], bcfg),
                                       "act_or_confirm": B.evidence(pol["act_or_confirm"], bcfg)}}
        return rep

    report = M.calibration_report(Ph, yh, setup.CLASSES, prior, note)
    report["directional_auc"] = directional_auc(Ph, yh)
    holdout_policy = P.evaluate(Ph, yh, rbh, rsh, segh, th, teh, sl, tp, spread[segh], cfg)
    verdict = P.verify(report["skill"]["has_skill"], holdout_policy, cfg)
    tr = P.tiers(Ph, sl, tp, spread[segh], cfg)
    every = np.ones(len(yh), bool)
    table = B.holdout_table(
        Ph, yh, P.non_overlapping(tr["tier"] >= 1, segh, th, teh), tr["side"],
        [data.segments[s]["symbol"] for s in segh], setup.sessions(Xh, data.feature_names, th),
        P.non_overlapping(every, segh, th, teh))
    report["policy"] = {
        "config": cfg.to_dict(), "tuning": tuning, "holdout": holdout_policy, "verdict": verdict,
        "bernoulli": {"act": B.evidence(holdout_policy["act"], bcfg),
                      "act_or_confirm": B.evidence(holdout_policy["act_or_confirm"], bcfg)},
        "sl_pips": sl, "tp_pips": tp, "horizon": H,
        "notes": ["win = the chosen side reached target 1 before its stop inside the horizon",
                  "mean_r is the sidecar's realised R net of the spread (a timed-out trade is closed at the "
                  "horizon)", "trades are non-overlapping per instrument; breakeven_rate = (sl + spread) / "
                  "(sl + tp), averaged over the trades taken",
                  "thresholds were tuned on validation rows that precede the holdout"]}

    by_segment, segments = {}, {}
    for s, info in enumerate(data.segments):
        m = segh == s
        entry = {**info, "n_train": int(seg_train[s]), "n_holdout": int(m.sum())}
        if m.sum() >= MIN_SLICE_HOLDOUT:
            rep = sliced(m)
            by_segment[info["key"]] = {**entry, **rep}
            entry["verdict"] = rep["policy"]["verdict"]
        else:
            entry["verdict"] = {"max_tier": "confirm", "reason": f"only {int(m.sum())} holdout rows for this "
                                                                 f"instrument; its edge is not verified"}
        segments[info["key"]] = entry
    report["by_segment"] = by_segment
    years = th.astype("datetime64[s]").astype("datetime64[Y]").astype(int) + 1970
    report["by_year"] = {str(int(yr)): sliced(years == yr) for yr in np.unique(years)
                         if (years == yr).sum() >= MIN_SLICE_HOLDOUT}

    def day(ts: int) -> str:
        return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    split = {"development_rows": int(len(yd)), "validation_rows": int(val_m.sum()), "holdout_rows": n_hold,
             "development_from": day(td.min()), "holdout_from": day(cut), "holdout_to": day(th.max()),
             "purged_rows": int(n - len(d_idx) - int((data.t >= cut).sum())),
             "stride": int(data.stride), "rows_available": int(data.rows_available),
             "max_train_rows": int(req.max_train_rows)}
    report["split"] = split
    summary = M.summary(report)
    summary.update({"directional_auc": report["directional_auc"], "calibration_method": calib["method"],
                    "stride": int(data.stride), "rows_available": int(data.rows_available),
                    "holdout_from": split["holdout_from"], "holdout_to": split["holdout_to"],
                    "policy": {"max_tier": verdict["max_tier"], "reason": verdict["reason"],
                               "act_margin": cfg.act_margin, "act_confidence": cfg.act_confidence,
                               "tuned": cfg.tuned, "holdout_act": holdout_policy["act"],
                               "holdout_act_or_confirm": holdout_policy["act_or_confirm"]}})
    return {
        "kind": "setup", "head": head, "interval": data.interval, "classes": list(setup.CLASSES), "model": model,
        "feature_version": data.feature_version, "feature_names": data.feature_names,
        "timeframes": data.timeframes, "feature_groups": data.feature_groups,
        "params": {"sl_pips": sl, "tp_pips": tp, "tp2_pips": float(req.tp2_pips), "horizon": H},
        "bernoulli_table": table, "segments": segments, "policy": cfg.to_dict(), "verdict": verdict, "report": report, "metrics": summary,
        "n_train": int(len(yd)), "n_feedback": n_fb, "calibration": calib, "trained_at": now_iso(),
        "split": split, "notes": data.notes + notes,
    }


def train_pattern(label: str, data: SetupData, req: Any,
                  progress: Callable[[str], None] = lambda m: None) -> dict:
    """One calibrated yes/no head for an outcome label, trained only on the rows where the label exists (the
    chart was in that situation). Same time split, purge and calibration as the setup head."""
    head = f"{label}:{data.interval}"
    j = data.label_names.index(label)
    lab = data.labels[:, j]
    exists = ~np.isnan(lab)
    H = int(req.horizon)
    dev_all, hold_all, cut = split_by_time(data.t, data.t_end, HOLDOUT_FRACTION)
    dev, hold = dev_all & exists, hold_all & exists
    y = np.where(exists, lab > 0.5, False).astype(np.int64)
    if dev.sum() < 300 or hold.sum() < 60:
        raise TrainError(f"{head}: {int(dev.sum())} development and {int(hold.sum())} holdout rows in this chart "
                         f"situation; too few to calibrate")
    if len(np.unique(y[dev])) < 2:
        raise TrainError(f"{head}: the development rows contain only one outcome")
    progress(f"{head}: fitting on {int(dev.sum())} rows where the pattern is on the chart")
    kw = dict(horizon=H, n_estimators=int(req.n_estimators), seed=int(req.seed), n_classes=2)
    try:
        model, calib = setup.fit(data.X[dev], y[dev], np.ones(int(dev.sum())), data.t[dev], data.t_end[dev], **kw)
    except setup.NotEnoughData as e:
        raise TrainError(f"{head}: {e}")
    ph, yh, segh, th = model.predict_proba(data.X[hold])[:, 1], y[hold], data.seg[hold], data.t[hold]
    base_rate = float(y[dev].mean())
    seg_rate = np.full(len(data.segments), base_rate)
    for s in range(len(data.segments)):
        m = dev & (data.seg == s)
        if m.sum() >= 50:
            seg_rate[s] = float(y[m].mean())
    prior = seg_rate[segh]

    def sliced(m: np.ndarray) -> dict:
        r = M.binary_report(ph[m], yh[m], prior[m])
        r["skill"] = M.skill_test((prior[m] - yh[m]) ** 2 - (ph[m] - yh[m]) ** 2)
        return r

    report = sliced(np.ones(len(yh), bool))
    report["n_holdout"] = report["n"]
    report["by_segment"] = {info["key"]: {"symbol": info["symbol"], "pip": info["pip"], **sliced(segh == s)}
                            for s, info in enumerate(data.segments) if (segh == s).sum() >= MIN_SLICE_HOLDOUT}
    years = th.astype("datetime64[s]").astype("datetime64[Y]").astype(int) + 1970
    report["by_year"] = {str(int(yr)): sliced(years == yr) for yr in np.unique(years)
                         if (years == yr).sum() >= MIN_SLICE_HOLDOUT}
    # Applicability gate: where did the label exist? Used at scoring time when the sidecar does not say.
    gate, gate_accuracy = None, None
    share = float(exists.mean())
    if 0 < exists[dev_all].sum() < dev_all.sum():
        gate = setup.booster(int(dev_all.sum()), min(100, int(req.n_estimators)), int(req.seed))
        gate.fit(data.X[dev_all], exists[dev_all].astype(int))
        gate_accuracy = M._f((gate.predict(data.X[hold_all]) == exists[hold_all].astype(int)).mean())
    summary = {"n_holdout": report["n"], "observed_rate": report["observed_rate"],
               "base_rate_train": report["base_rate_train"], "log_loss": report["log_loss"],
               "brier": report["brier"], "ece": report["ece"], "baseline_log_loss": report["baseline"]["log_loss"],
               "baseline_brier": report["baseline"]["brier"], "brier_skill_score": report["brier_skill_score"],
               "has_skill": report["skill"]["has_skill"], "calibration_method": calib["method"],
               "applicable_share": M._f(share), "applicability_gate_accuracy": gate_accuracy,
               "stride": int(data.stride)}
    return {"kind": "pattern", "head": head, "label": label, "interval": data.interval, "classes": [],
            "model": model, "gate": gate, "feature_version": data.feature_version,
            "feature_names": data.feature_names, "timeframes": data.timeframes,
            "feature_groups": data.feature_groups, "doc": data.label_docs.get(label, ""),
            "params": {"sl_pips": float(req.sl_pips), "tp_pips": float(req.tp_pips),
                       "tp2_pips": float(req.tp2_pips), "horizon": H},
            "report": report, "metrics": summary, "n_train": int(dev.sum()), "n_feedback": 0,
            "calibration": calib, "trained_at": now_iso(),
            "spec": {"label": label, "interval": data.interval, "doc": data.label_docs.get(label, "")},
            "notes": [f"trained on the {share:.1%} of rows where '{label}' is defined"]}


def _expand(mask: np.ndarray, where: np.ndarray) -> np.ndarray:
    """A mask over the rows selected by `where`, as a mask over all rows."""
    out = np.zeros(len(where), bool)
    out[np.flatnonzero(where)[mask]] = True
    return out


# ----------------------------------------------------------------------------- intent rows

def intent_rows(head: str, seed: int) -> tuple[list[dict], list[int], list[str]]:
    classes = base.SPECS[head].classes
    key = "intent" if head == "intent" else "style"
    rows = [r for r in seeds.generate(seed) if r.get(key) in classes]
    return ([intent.normalize_state(r) for r in rows], [classes.index(r[key]) for r in rows],
            [r["group"] for r in rows])


def split_by_template(labels: list[int], groups: list[str], seed: int) -> np.ndarray:
    """Holdout mask: 20% of the templates of each class (the user's own phrasings always stay in training)."""
    rng = np.random.default_rng(seed)
    labels_a = np.array(labels)
    groups_a = np.array(groups)
    hold = np.zeros(len(labels), bool)
    for c in np.unique(labels_a):
        gs = sorted({g for g, l in zip(groups, labels) if l == c and not g.startswith("pinned:")})
        rng.shuffle(gs)
        take = set(gs[:max(1, round(len(gs) * HOLDOUT_FRACTION))])
        hold |= (labels_a == c) & np.isin(groups_a, list(take))
    return hold


def train_intent(head: str, store: Store, req: Any) -> dict:
    spec = base.SPECS[head]
    states, labels, groups = intent_rows(head, int(req.seed))
    hold = split_by_template(labels, groups, int(req.seed))
    tr = np.flatnonzero(~hold)
    Xtr, ytr, wtr = [states[i] for i in tr], [labels[i] for i in tr], [1.0] * len(tr)
    n_fb, skipped = 0, 0
    for r in store.feedback(head):
        if r.get("label") not in spec.classes:
            skipped += 1
            continue
        try:
            Xtr.append(intent.normalize_state(r.get("state")))
        except Exception:
            skipped += 1
            continue
        ytr.append(spec.classes.index(r["label"]))
        wtr.append(float(r.get("weight") or 1.0) * float(req.feedback_weight))
        n_fb += 1
    # feedback rows arrive last; shuffle so the stratified calibration folds each see some of them
    order = np.random.default_rng(int(req.seed)).permutation(len(ytr))
    model = intent.fit([Xtr[i] for i in order], [ytr[i] for i in order], [wtr[i] for i in order], int(req.seed))
    yh = np.array([labels[i] for i in np.flatnonzero(hold)])
    Ph = model.predict_proba(intent.as_array([states[i] for i in np.flatnonzero(hold)]))
    prior = np.bincount(np.array(ytr[:len(tr)]), minlength=len(spec.classes)) / len(tr)
    report = M.calibration_report(Ph, yh, spec.classes, prior)
    report["data_note"] = ("Holdout rows are synthetic seed phrasings from templates that were not in training. "
                           "This is a sanity check, not accuracy on real traffic. The served model is then "
                           "refitted on all seed templates plus feedback, so these numbers describe a model "
                           "that saw 20% fewer templates than the one answering.")
    summary = M.summary(report)
    summary["holdout_templates"] = int(len({groups[i] for i in np.flatnonzero(hold)}))
    # serve a model that has seen every template: the held-out ones were only withheld to measure
    for i in np.flatnonzero(hold):
        Xtr.append(states[i]); ytr.append(labels[i]); wtr.append(1.0)
    order = np.random.default_rng(int(req.seed)).permutation(len(ytr))
    model = intent.fit([Xtr[i] for i in order], [ytr[i] for i in order], [wtr[i] for i in order], int(req.seed))
    return {"kind": "intent", "head": head, "classes": list(spec.classes), "model": model, "report": report,
            "metrics": summary, "n_train": int(len(ytr)), "n_seed": int(len(states)), "n_feedback": n_fb,
            "trained_at": now_iso(),
            "notes": [f"{skipped} feedback rows skipped (label not in classes or bad state)"] if skipped else []}


# ----------------------------------------------------------------------------- jobs

class Busy(Exception):
    def __init__(self, job_id: str):
        super().__init__(job_id)
        self.job_id = job_id


class Trainer:
    """One training job at a time, in a background thread. Job state lives in memory."""

    def __init__(self, store: Store, quant: Callable[[], QuantClient]):
        self.store, self.quant = store, quant
        self.jobs: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    def plan(self, req: Any) -> list[dict]:
        """Tasks of a request: one per intent head, one per interval (its setup head and/or the pattern heads
        discovered in that interval's data). Derived noul heads map to the setup model behind them."""
        for iv in req.intervals or []:
            if iv not in base.INTERVALS:
                raise TrainError(f"interval {iv!r} is not supported; choose from {list(base.INTERVALS)}")
        if req.quant_url:
            try:
                check_override(req.quant_url)
            except ValueError as e:
                raise TrainError(str(e))
        tasks: dict[str, dict] = {}

        def interval_task(iv: str) -> dict:
            return tasks.setdefault(f"interval:{iv}", {"kind": "interval", "interval": iv, "setup": False,
                                                       "labels": set()})
        if not req.heads:
            tasks = {h: {"kind": "intent", "head": h} for h in ("intent", "trade_style")}
            for iv in (req.intervals or base.INTERVALS):
                interval_task(iv).update(setup=True, labels=None if req.patterns else set())
            return list(tasks.values())
        for h in req.heads:
            spec = base.SPECS.get(h)
            label, _, iv = h.rpartition(":")
            if spec is not None and spec.kind == "intent":
                tasks[h] = {"kind": "intent", "head": h}
            elif iv in base.INTERVALS and (not req.intervals or iv in req.intervals):
                task = interval_task(iv)
                if spec is not None:          # setup:<iv> or a noul derived from it
                    task["setup"] = True
                    if req.patterns and task["labels"] is not None and not task["labels"]:
                        task["labels"] = None  # None = every label the data offers
                elif task["labels"] is not None:
                    task["labels"].add(label)
            elif iv not in base.INTERVALS:
                raise TrainError(f"unknown head {h!r}; available: {base.available()} plus <label>:<interval> "
                                 f"for the outcome labels the sidecar exports")
        return list(tasks.values())

    def running(self) -> Optional[dict]:
        return next((j for j in self.jobs.values() if j["status"] in ("queued", "running")), None)

    def start(self, req: Any, wait: bool = False) -> dict:
        tasks = self.plan(req)
        if not tasks:
            raise TrainError("nothing to train")
        with self._lock:
            running = self.running()
            if running:
                raise Busy(running["job_id"])
            names = [t["head"] if t["kind"] == "intent" else f"setup:{t['interval']}" for t in tasks
                     if t["kind"] == "intent" or t["setup"]]
            job = {"job_id": uuid.uuid4().hex[:12], "status": "queued", "created_at": now_iso(),
                   "started_at": None, "finished_at": None, "request": req.model_dump(),
                   "progress": {"done": 0, "total": len(tasks), "message": "queued"},
                   "heads": {k: {"status": "pending"} for k in names}, "model": None, "error": None}
            self.jobs[job["job_id"]] = job
        if wait:
            self._run(job, req, tasks)
        else:
            self._thread = threading.Thread(target=self._run, args=(job, req, tasks), daemon=True,
                                            name=f"rlcd-train-{job['job_id']}")
            self._thread.start()
        return job

    def get(self, job_id: str) -> Optional[dict]:
        return self.jobs.get(job_id)

    def _run(self, job: dict, req: Any, tasks: list[dict]) -> None:
        job["status"], job["started_at"] = "running", now_iso()

        def say(msg: str) -> None:
            job["progress"]["message"] = msg

        artifacts: dict[str, dict] = {}

        def attempt(name: str, fn: Callable[[], dict]) -> None:
            say(f"training {name}")
            try:
                art = fn()
                artifacts[name] = art
                job["heads"][name] = {"status": "trained", "n_train": art["n_train"],
                                      "n_feedback": art.get("n_feedback", 0), "metrics": art["metrics"],
                                      "notes": art.get("notes", [])}
            except (TrainError, QuantUnavailable, QuantRejected, setup.NotEnoughData) as e:
                job["heads"][name] = {"status": "failed", "error": str(e)}
            except Exception as e:  # keep the other heads going; the trace goes to the job
                job["heads"][name] = {"status": "failed", "error": f"{type(e).__name__}: {e}",
                                      "trace": traceback.format_exc()[-1500:]}

        try:
            for task in tasks:
                if task["kind"] == "intent":
                    attempt(task["head"], lambda: train_intent(task["head"], self.store, req))
                else:
                    iv, data = task["interval"], None
                    try:
                        quant = self.quant()
                        if req.quant_url:
                            quant = quant.with_url(check_override(req.quant_url))
                        symbols = [s.upper() for s in req.symbols] if req.symbols else quant.default_symbols()
                        if not symbols:
                            raise TrainError("the quant sidecar offers none of the default symbols")
                        pips = {**DEFAULT_PIPS, **{k.upper(): v for k, v in (req.pips or {}).items()}}
                        data = load_setup_data(quant, self.store, iv, symbols, pips, req, say)
                    except Exception as e:   # a failed export must not cost the heads already trained
                        expected = isinstance(e, (TrainError, QuantUnavailable, QuantRejected))
                        job["heads"][f"setup:{iv}" if task["setup"] else f"interval:{iv}"] = {
                            "status": "failed", "error": str(e) if expected else f"{type(e).__name__}: {e}",
                            **({} if expected else {"trace": traceback.format_exc()[-1500:]})}
                    if data is not None:
                        if task["setup"]:
                            attempt(f"setup:{iv}", lambda: train_setup(f"setup:{iv}", data, self.store, req, say))
                        wanted = data.label_names if task["labels"] is None else sorted(task["labels"])
                        for label in wanted:
                            if label not in data.label_names:
                                job["heads"][f"{label}:{iv}"] = {
                                    "status": "failed", "error": f"the sidecar exports no label {label!r} for "
                                                                 f"{iv}; it offers {data.label_names}"}
                                continue
                            attempt(f"{label}:{iv}", lambda: train_pattern(label, data, req, say))
                        del data
                job["progress"]["done"] += 1
            if artifacts:
                tables = {k: a.pop("bernoulli_table") for k, a in artifacts.items() if "bernoulli_table" in a}
                job["model"] = self.store.commit(artifacts, [f"job {job['job_id']}"])
                for head, table in tables.items():   # the Bernoulli ledger restarts from the new holdout
                    self.store.ledger.reset(head, {**table, "model_version": job["model"]})
            failed = [k for k, v in job["heads"].items() if v["status"] == "failed"]
            job["status"] = "succeeded" if not failed else ("partial" if artifacts else "failed")
            if failed:
                job["error"] = "; ".join(f"{k}: {job['heads'][k]['error']}" for k in failed)
            say("done" if not failed else f"done with {len(failed)} failed head(s)")
        except Exception as e:
            job["status"], job["error"] = "failed", f"{type(e).__name__}: {e}"
        finally:
            job["finished_at"] = now_iso()
