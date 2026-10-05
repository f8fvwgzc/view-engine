"""A synthetic quant sidecar for tests: same wire contract (/dataset as JSON and npz, /features, /symbols,
/health), served through an httpx.MockTransport, so no test touches the network.

`signal` plants an edge: the outcome distribution of a row depends on two of its features. signal=0 is pure
noise (outcomes independent of every feature).
"""
from __future__ import annotations

import io
import json
import zlib
from datetime import datetime, timezone

import httpx
import numpy as np

NAMES = ["tr_swing_trend", "ht1_swing_trend", "ret4_atr", "sl_atr", "atr_pips", "hour_sin", "tr_pullback_dir",
         "noise_b"]
GROUPS = {"tr_swing_trend": "structure", "ht1_swing_trend": "structure", "ret4_atr": "strength",
          "sl_atr": "volatility", "atr_pips": "volatility", "hour_sin": "session", "tr_pullback_dir": "structure",
          "noise_b": "candle"}
LABEL = "cont_after_pullback"   # defined only while a pullback is on the chart (tr_pullback_dir != 0)
STEP = {"5m": 300, "15m": 900, "1h": 3600}
T0 = int(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp())
BULL = [2.5, 1.0, 2.5, 1.2, 30.0, 0.0, 1.0, 0.0]          # strong buy, in an uptrend pullback
BULL_NO_PATTERN = [2.5, 1.0, 2.5, 1.2, 30.0, 0.0, 0.0, 0.0]


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SynthQuant:
    def __init__(self, signal: float = 2.0, rows: int = 2500, feature_version: str = "synth-v1",
                 npz: bool = True, symbols=("XAUUSD", "EURUSD", "GBPUSD", "DXY"), up: bool = True,
                 labels: bool = True):
        self.signal, self.rows, self.feature_version, self.npz = signal, rows, feature_version, npz
        self.labels = labels                 # export the extra outcome label (a "structure-v2"-style feed)
        self.declare: dict | None = None     # /features `applicable` map, when the sidecar states it
        self.damage = 0                      # serve this many npz exports with the right size and wrong bytes
        self.fail_500 = 0                    # answer this many npz exports with HTTP 500
        self.symbols, self.up = list(symbols), up
        self.next_x: list | None = None      # what /features returns as x (default: the last dataset row)
        self.calls: list[tuple[str, dict]] = []

    def client_transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ data
    def default_pip(self, symbol: str) -> float:
        return 0.1 if symbol == "XAUUSD" else 0.0001

    def build(self, symbol: str, interval: str, pip: float, sl: float, tp: float, horizon: int) -> dict:
        rng = np.random.default_rng(zlib.crc32(f"{symbol}|{interval}|{pip:g}".encode()))
        n = self.rows
        X = rng.normal(size=(n, len(NAMES))).astype(np.float32)
        X[:, 3] = rng.uniform(0.3, 2.0, n)
        X[:, 4] = rng.uniform(5, 60, n)
        X[:, 6] = rng.choice([-1.0, 0.0, 0.0, 0.0, 1.0], n)
        X[rng.random(n) < 0.03, 7] = np.nan
        s = self.signal * (X[:, 0] + 0.5 * X[:, 2])
        a = np.log(0.28 / 0.44)
        logits = np.stack([a + s, a - s, np.zeros(n)], axis=1)
        prob = np.exp(logits) / np.exp(logits).sum(axis=1, keepdims=True)
        u = rng.random(n)
        cls = np.where(u < prob[:, 0], 0, np.where(u < prob[:, 0] + prob[:, 1], 1, 2))  # 0 buy 1 sell 2 hold
        spread = 2.5 if (symbol, pip) == ("XAUUSD", 0.1) else (0.25 if symbol == "XAUUSD" else 1.0)
        cost = spread / sl
        timeout = rng.normal(0, 0.3, n)
        labelled = np.arange(n) < n - horizon
        long_r = np.where(cls == 0, tp / sl, np.where(u > 0.9, timeout, -1.0)) - cost
        short_r = np.where(cls == 1, tp / sl, np.where(u > 0.9, timeout, -1.0)) - cost
        times = T0 + np.arange(n) * STEP[interval]
        on = X[:, 6] != 0   # the pattern label exists only in a pullback; it continues when momentum agrees
        cont = rng.random(n) < 1 / (1 + np.exp(-self.signal * X[:, 2] * X[:, 6]))
        pattern = np.where(on & labelled, cont.astype(float), np.nan)
        extra = {"label_names": ["long_tp1", "short_tp1", LABEL],
                 "label_docs": {LABEL: "after a pullback, price continues in the trend direction"},
                 "feature_groups": GROUPS} if self.labels else {}
        return {"X": X, "pattern": pattern, "cls": cls, "labelled": labelled, "long_r": long_r, "short_r": short_r, "times": times,
                "meta": {"symbol": symbol, "interval": interval, "pip": pip,
                         "params": {"sl_pips": sl, "tp_pips": tp, "tp2_pips": 100.0, "horizon": horizon,
                                    "spread_pips": spread},
                         "feature_version": self.feature_version, "feature_names": NAMES,
                         "timeframes": {"tr": interval, "ht1": "1h", "ht2": "4h"}, "source": "synthetic",
                         "n": n, "labelled": int(labelled.sum()), "start": iso(times[0]), "end": iso(times[-1]),
                         "notes": ["synthetic"], **extra}}

    # ------------------------------------------------------------------ http
    def handle(self, request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        path = request.url.path
        self.calls.append((path, q))
        if not self.up:
            raise httpx.ConnectError("connection refused", request=request)
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/symbols":
            return httpx.Response(200, json={"count": len(self.symbols),
                                             "symbols": [{"id": s, "asset_class": "fx"} for s in self.symbols]})
        symbol = q.get("symbol", "").upper()
        if symbol not in self.symbols:
            return httpx.Response(400, json={"error": f"cannot interpret symbol '{symbol}'", "kind": "bad_symbol"})
        if symbol == "DXY":
            return httpx.Response(404, json={"error": "no intraday data for DXY", "kind": "no_data"})
        interval = q.get("interval", "15m")
        pip = float(q["pip"]) if q.get("pip") else self.default_pip(symbol)
        sl, tp = float(q.get("sl_pips", 20)), float(q.get("tp_pips", 50))
        horizon = int(q.get("horizon", 48))
        d = self.build(symbol, interval, pip, sl, tp, horizon)
        m = d["meta"]
        if path == "/features":
            x = self.next_x if self.next_x is not None else [None if np.isnan(v) else float(v) for v in d["X"][-1]]
            return httpx.Response(200, json={
                "symbol": symbol, "interval": interval, "time": iso(d["times"][-1]), "price": 2400.0, "pip": pip,
                "params": m["params"], "feature_version": self.feature_version, "feature_names": NAMES,
                "timeframes": m["timeframes"], "x": x,
                "facts": {"volatility": {"sl_atr": x[3]}, "alignment": {"up": 2}}, "data_note": "synthetic",
                **({"applicable": self.declare} if self.declare is not None else {})})
        if path == "/dataset":
            if q.get("format") == "npz" and self.npz and self.fail_500 > 0:
                self.fail_500 -= 1
                return httpx.Response(500, json={"error": "export collided", "kind": "internal"})
            if q.get("format") == "npz" and self.npz:
                best = np.where(d["labelled"], np.select([d["cls"] == 0, d["cls"] == 1], [1, 2], 0), -1)
                nan = np.where(d["labelled"], 0.0, np.nan)
                buf = io.BytesIO()
                np.savez(buf, X=d["X"], times=d["times"].astype(np.int64), best=best.astype(np.int8),
                         long_tp1=(d["cls"] == 0) + nan, short_tp1=(d["cls"] == 1) + nan,
                         long_r=(d["long_r"] + nan).astype(np.float32),
                         short_r=(d["short_r"] + nan).astype(np.float32), meta=np.array(json.dumps(m)),
                         **({LABEL: d["pattern"].astype(np.float32)} if self.labels else {}))
                body = buf.getvalue()
                if self.damage > 0:
                    self.damage -= 1
                    half = len(body) // 2
                    body = body[:half] + bytes(len(body) - half)
                return httpx.Response(200, content=body, headers={"content-type": "application/octet-stream"})
            lo = max(0, self.rows - int(q.get("max_rows", 20000)))

            def vec(a):
                return [None if not ok else float(v) for v, ok in zip(a[lo:], d["labelled"][lo:])]
            best = [("buy", "sell", "hold")[c] if ok else None for c, ok in zip(d["cls"][lo:], d["labelled"][lo:])]
            X = [[None if np.isnan(v) else float(v) for v in row] for row in d["X"][lo:]]
            return httpx.Response(200, json={
                **m, "times": [iso(t) for t in d["times"][lo:]], "X": X,
                "y": {"long_tp1": vec(d["cls"] == 0), "short_tp1": vec(d["cls"] == 1), "long_r": vec(d["long_r"]),
                      "short_r": vec(d["short_r"]), "best": best,
                      **({LABEL: [None if np.isnan(v) else float(v) for v in d["pattern"][lo:]]}
                         if self.labels else {})}})
        return httpx.Response(404, json={"detail": "Not Found"})
