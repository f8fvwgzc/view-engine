"""RLCD — FastAPI app.

Run: cd backend/rlcd && uv run uvicorn rlcd.api:app --host 127.0.0.1 --port 8095
"""
from __future__ import annotations

import hmac
import logging
import math
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import numpy as np
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import LATEST, VERSION
from . import bernoulli as B
from . import policy as P
from .contract import (MAX_CANDIDATES, ContractError, DecideRequest, FeedbackRequest, Question, RankRequest,
                       ScanRequest, SystemOneRequest, TrainRequest, build_answer, noul_answer, parse_questions,
                       positive_probability)
from .heads import base, intent, setup
from .heads.base import HeadSpec
from .quant import MAJORS, QuantClient, QuantRejected, QuantUnavailable
from .store import Store, UnknownModel
from .training import Busy, TrainError, Trainer, parse_time, segment_key

log = logging.getLogger("rlcd")
SMALL_STOP_ATR = 0.5
COMPOSITE = P.CompositeConfig()


def error(status: int, message: str, kind: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"error": message, "kind": kind, **extra}, status_code=status)


def feature_row(f: dict) -> np.ndarray:
    return np.array([[np.nan if v is None else float(v) for v in f.get("x") or []]], np.float32)


class Service:
    """Everything the endpoints need: the artifact store, the sidecar client and the trainer."""

    def __init__(self, home: Optional[Path] = None, quant: Optional[QuantClient] = None):
        self.store = Store(home)
        self._quant = quant
        self.trainer = Trainer(self.store, lambda: self.quant)
        self.rng = np.random.default_rng()

    @property
    def quant(self) -> QuantClient:
        if self._quant is None:
            self._quant = QuantClient()
        return self._quant

    # ------------------------------------------------------------------ lookups
    def resolve(self, model: Optional[str]) -> str:
        try:
            return self.store.resolve(model)
        except UnknownModel:
            reg = self.store.registry()
            raise ContractError(f"unknown model {model!r}", kind="unknown_model",
                                available=[LATEST, *sorted(reg["versions"])])

    def specs(self, version: str) -> dict[str, HeadSpec]:
        """Built-in heads plus the pattern heads this version discovered in its training data."""
        out = dict(base.SPECS)
        for name, info in self.store.version(version).get("heads", {}).items():
            spec = base.spec_from_registry(name, info)
            if spec is not None:
                out[name] = spec
        return out

    def spec(self, head: str, version: str, question: Optional[str] = None) -> HeadSpec:
        specs = self.specs(version)
        if head not in specs:
            raise ContractError(
                f"no head named {head!r}. RLCD only answers questions bound to a trained head: set the "
                f"question's `head` field (or use a head name as the question id).", kind="unknown_head",
                question=question, available_heads=list(specs))
        return specs[head]

    # ------------------------------------------------------------------ answering
    def features(self, artifact: Optional[dict], spec: HeadSpec, state: dict, memo: dict) -> dict:
        iv = state.get("interval")
        if iv is not None and str(iv) != spec.interval:
            raise ContractError(f"state.interval is {iv!r} but head '{spec.name}' is the {spec.interval} head",
                                head=spec.name)
        p = (artifact or {}).get("params", {})
        key = (str(state["symbol"]), spec.interval, state.get("as_of"), state.get("pip"))
        if key not in memo:
            memo[key] = self.quant.features(key[0], spec.interval, as_of=key[2], pip=key[3],
                                            sl_pips=p.get("sl_pips"), tp_pips=p.get("tp_pips"),
                                            tp2_pips=p.get("tp2_pips"), horizon=p.get("horizon"))
        return memo[key]

    def vector(self, artifact: Optional[dict], spec: HeadSpec, state: Any, memo: dict):
        """(feature row checked against the head's feature version, what the sidecar declared applicable).
        The row is None when the head is untrained."""
        if not isinstance(state, dict) or not ("x" in state or "symbol" in state):
            raise ContractError(f"head '{spec.name}' needs `state` to be either {{x, feature_names, "
                                f"feature_version}} or {{symbol, interval?, as_of?}}", head=spec.name)
        if "x" in state:
            if "feature_version" not in state:
                raise ContractError("state.feature_version is required with state.x", head=spec.name)
            if artifact is None:
                return None, None
            return (setup.check_vector(artifact, state["feature_version"], state.get("feature_names"),
                                       state["x"]), state.get("applicable"))
        if artifact is None:
            return None, None
        f = self.features(artifact, spec, state, memo)
        return (setup.check_vector(artifact, f.get("feature_version"), f.get("feature_names"), f.get("x")),
                f.get("applicable"))

    @staticmethod
    def pattern_answer(artifact: dict, spec: HeadSpec, x: np.ndarray, declared: Any) -> dict:
        """A pattern head only answers when its pattern is on the chart."""
        if not setup.applicable(artifact, x, declared):
            return {"type": "noul", "noul": None, "confidence": None, "head": spec.name, "calibrated": True,
                    "applicable": False}
        p = float(artifact["model"].predict_proba(x)[0, 1])
        return {**noul_answer(p, head=spec.name, calibrated=True), "applicable": True}

    def answer(self, version: str, q: Question, state: Any, memo: dict, warnings: list[str]) -> dict:
        spec = self.spec(q.head, version, q.id)
        q.bind(spec)
        artifact = self.store.artifact(version, spec.artifact)
        probs = None
        if spec.kind == "intent":
            s = intent.normalize_state(state)
            probs = None if artifact is None else intent.predict(artifact, s)
        else:
            x, declared = self.vector(artifact, spec, state, memo)
            if x is not None and spec.kind == "pattern":
                return self.pattern_answer(artifact, spec, x, declared)
            if x is not None:
                probs = setup.predict(artifact, x)
                if artifact["verdict"]["max_tier"] == "hold":
                    msg = f"head '{spec.name}': {artifact['verdict']['reason']}"
                    if msg not in warnings:
                        warnings.append(msg)
        if probs is None:
            msg = (f"head '{spec.name}' is not trained in {version}: the answer is the uninformed prior "
                   f"(calibrated=false). Train it with POST /v1/train.")
            if msg not in warnings:
                warnings.append(msg)
            k = len(spec.classes)
            return build_answer(q, spec, [0.5] if spec.type == "noul" else [1.0 / k] * k, calibrated=False)
        if spec.type == "noul":
            probs = [probs[setup.LABEL[spec.positive]]]
        return build_answer(q, spec, probs, calibrated=True)

    # ------------------------------------------------------------------ views
    def head_view(self, version: str, spec: HeadSpec, pending: dict) -> dict:
        info = self.store.version(version).get("heads", {}).get(spec.artifact)
        out = {"name": spec.name, "type": spec.type, "classes": list(spec.classes), "positive": spec.positive,
               "kind": spec.kind, "description": spec.description, "input": spec.input,
               "trainable": spec.trainable, "backed_by": spec.artifact, "trained": info is not None,
               "n_train": None, "trained_at": None, "model_version": None, "metrics": None,
               "feedback_rows": pending.get(spec.name, 0)}
        if info:
            out.update(n_train=info.get("n_train"), trained_at=info.get("trained_at"),
                       model_version=info.get("model_version"), metrics=info.get("metrics"))
        return out

    # ------------------------------------------------------------------ the day-trade decision
    def decide(self, req: DecideRequest) -> dict:
        if req.interval not in base.INTERVALS:
            raise ContractError(f"interval must be one of {list(base.INTERVALS)}", intervals=list(base.INTERVALS))
        version = self.resolve(req.model)
        spec = base.SPECS[f"setup:{req.interval}"]
        artifact = self.store.artifact(version, spec.artifact)
        trained = (artifact or {}).get("params", {})
        f = self.quant.features(req.symbol, req.interval, as_of=req.as_of, pip=req.pip, sl_pips=req.sl_pips,
                                tp_pips=req.tp_pips, tp2_pips=req.tp2_pips, horizon=trained.get("horizon"))
        symbol, price, pip = f.get("symbol", req.symbol), float(f["price"]), float(f["pip"])
        names, row = list(f.get("feature_names") or []), feature_row(f)
        cfg = P.PolicyConfig.from_dict((artifact or {}).get("policy"))
        bcfg = cfg.bernoulli()
        seg = (artifact or {}).get("segments", {}).get(segment_key(symbol, pip))
        cost = req.cost_pips
        if cost is None:
            cost = (f.get("params") or {}).get("spread_pips")
        if cost is None:
            cost = seg["spread_pips"] if seg else cfg.default_cost_pips
        cost = float(cost)
        warnings, max_tier, x, calibration = [], "act", None, None

        def cap(tier: str, why: str) -> None:
            nonlocal max_tier
            if P.TIERS.index(tier) < P.TIERS.index(max_tier):
                max_tier = tier
            warnings.append(why)

        if artifact is None:
            probs = np.full(3, 1 / 3)
            cap("hold", f"head '{spec.name}' is not trained in {version}: no signal. Train it with POST /v1/train.")
        else:
            x = setup.check_vector(artifact, f.get("feature_version"), names, f.get("x"))
            probs = setup.predict(artifact, x)
            verdict = artifact["verdict"]
            if verdict["max_tier"] != "act":
                cap(verdict["max_tier"], f"pooled holdout: {verdict['reason']}")
            if seg is None:
                cap("confirm", f"{symbol} at pip {pip:g} was not in the training set; the holdout says nothing "
                               f"about it, so the decision is capped at confirm")
            elif seg["verdict"]["max_tier"] != "act":
                cap(seg["verdict"]["max_tier"], f"{symbol} at pip {pip:g}: {seg['verdict']['reason']}")
            if (req.sl_pips, req.tp_pips) != (trained["sl_pips"], trained["tp_pips"]):
                cap("hold", f"the model was trained for stop {trained['sl_pips']:g} / target "
                            f"{trained['tp_pips']:g} pips; its probabilities do not apply to "
                            f"{req.sl_pips:g} / {req.tp_pips:g}")
            by_seg = artifact["report"].get("by_segment", {})
            calibration = {**artifact["metrics"], "model_version": artifact.get("model_version"),
                           "trained_at": artifact.get("trained_at"), "pooled_verdict": verdict,
                           "instrument": segment_summary(by_seg[seg["key"]]) if seg and seg["key"] in by_seg
                           else None}
        sl_atr = ((f.get("facts") or {}).get("volatility") or {}).get("sl_atr")
        if sl_atr is None and "sl_atr" in names and not np.isnan(row[0, names.index("sl_atr")]):
            sl_atr = float(row[0, names.index("sl_atr")])
        if sl_atr is not None and sl_atr < SMALL_STOP_ATR:
            warnings.append(f"stop is smaller than half a candle's range (stop = {sl_atr:.2f} ATR of the "
                            f"{req.interval} candle): outcome is mostly noise at this timeframe")
        try:
            session = setup.sessions(row, names, np.array([parse_time(f["time"])]))[0]
        except Exception:
            session = "unknown"

        pb, ps, ph = (float(v) for v in probs)
        d = P.decide(pb, ps, ph, req.sl_pips, req.tp_pips, cost, cfg, max_tier)
        side, p0 = d["lean"], d["breakeven_probability"]
        p = pb if side == "buy" else ps

        # --- the Bernoulli view of this trade, and the posterior gate on `act`
        table = self.store.ledger.table(spec.name)
        live = artifact is not None and table.get("model_version") == artifact.get("model_version")
        k = B.bin_of(p)
        wins, losses = table["bins"][side][k] if live else (0, 0)
        post = B.posterior(wins, losses, p0, bcfg)
        cell = table["symbol_session"].get(f"{symbol}|{session}") if live else None
        gate = {"threshold": cfg.min_p_above_breakeven, "mode": "posterior",
                "passed": post["p_above_breakeven"] >= cfg.min_p_above_breakeven}
        if req.explore:
            draw = B.thompson(wins, losses, self.rng, bcfg)
            gate = {"threshold": cfg.min_p_above_breakeven, "mode": "thompson_sampling", "exploration": True,
                    "sampled_win_rate": round(draw, 6), "passed": draw > p0}
        if d["tier"] == "act" and not gate["passed"]:
            d["tier"] = "confirm"
            warnings.append(
                f"Bernoulli gate: predictions like this one (P({side}) in {k / 10:.1f}-{(k + 1) / 10:.1f}) have "
                f"won {wins} of {wins + losses}; P(true win rate > breakeven {p0:.3f}) = "
                f"{post['p_above_breakeven']:.3f} is below {cfg.min_p_above_breakeven:g}, so act becomes confirm")
        if req.explore:
            warnings.append("exploration mode: the Bernoulli gate was decided by a draw from the win-rate "
                            "posterior (Thompson sampling), not by its mass above breakeven")
        if d["tier"] == "confirm":
            warnings.append(f"confirm: lean {side}, wait for the trigger candle before entering")
        payoff = req.tp_pips / req.sl_pips
        net = (req.tp_pips - cost) / (req.sl_pips + cost)
        size = B.sizing(p, net, bcfg)
        bernoulli = {
            "side": side, "p": round(p, 6), "payoff_b": round(payoff, 6), "payoff_b_net": round(net, 6),
            "cost_r": round(cost / req.sl_pips, 6), "breakeven": p0,
            "expected_r": d["expected_r"][side], "variance_r": round(B.variance_r(p, payoff), 6),
            "std_r": round(math.sqrt(B.variance_r(p, payoff)), 6), **size,
            "stake_fraction": size["kelly_quarter"] if d["tier"] == "act" else 0.0,
            "posterior": {**post, "wins": wins, "losses": losses, "bin": [k / 10, (k + 1) / 10],
                          "of": f"{spec.name} predictions with P({side}) in this bin, holdout + scored outcomes"},
            "symbol_session": None if cell is None else {
                "key": f"{symbol}|{session}", "wins": cell[0], "losses": cell[1],
                **B.posterior(cell[0], cell[1], p0, bcfg)},
            "gate": gate, "trades_needed_to_confirm": B.trades_needed(p, p0, bcfg),
            "note": "kelly uses the payoff net of the spread; kelly_quarter is the capped fraction of the "
                    "account to risk and stake_fraction is 0 unless the tier is act",
        }

        def levels(s: str) -> dict:
            sign = 1.0 if s == "buy" else -1.0
            return {"entry": price, "stop": price - sign * req.sl_pips * pip,
                    "targets": [price + sign * req.tp_pips * pip, price + sign * req.tp2_pips * pip]}
        both = {"buy": levels("buy"), "sell": levels("sell")}
        action = side if d["tier"] != "hold" else "hold"
        chosen = both.get(action, {"entry": price, "stop": None, "targets": []})

        # --- atomic nouls and the composite built from them
        nouls, off_pattern = {}, []
        if artifact is not None:
            nouls["long_tp1"] = {**noul_answer(pb, head=f"long_tp1:{req.interval}", calibrated=True),
                                 "applicable": True}
            nouls["short_tp1"] = {**noul_answer(ps, head=f"short_tp1:{req.interval}", calibrated=True),
                                  "applicable": True}
        for name, pspec in self.specs(version).items():
            if pspec.kind != "pattern" or pspec.interval != req.interval:
                continue
            part = self.store.artifact(version, pspec.artifact)
            if part is None:
                continue
            try:
                px = setup.check_vector(part, f.get("feature_version"), names, f.get("x"))
            except ContractError as e:
                warnings.append(f"noul '{name}' skipped: {e.message}")
                continue
            ans = self.pattern_answer(part, pspec, px, f.get("applicable"))
            if ans["applicable"]:
                nouls[pspec.positive] = ans
            else:
                off_pattern.append(pspec.positive)
        directions = {}
        for label, col in COMPOSITE.direction_feature.items():
            if col in names and not np.isnan(row[0, names.index(col)]) and row[0, names.index(col)] != 0:
                directions[label] = 1 if row[0, names.index(col)] > 0 else -1
        composite = P.composite({k_: v["noul"] for k_, v in nouls.items()}, directions, COMPOSITE)

        # --- why: top features and the share of the attribution by feature group
        reasons, groups = [{"text": "no trained model: there are no model reasons; see `facts`"}], {}
        if artifact is not None:
            contrib = setup.contributions(artifact, x, setup.LABEL[side])
            if contrib is None:
                reasons = [{"text": "feature attributions are unavailable for this model; see `facts`"}]
            else:
                roles = f.get("timeframes") or artifact.get("timeframes") or {}
                reasons = render_reasons(setup.top_features(artifact, x, contrib), side, roles,
                                         f.get("feature_docs"))
                groups = setup.group_shares(artifact, contrib)
        style = None
        if req.text:
            style_art = self.store.artifact(version, "trade_style")
            if style_art is not None:
                sp = intent.predict(style_art, {"text": req.text, "symbol": symbol, "timeframes": [req.interval]})
                style = build_answer(Question("trade_style", {"type": "choice"}), base.SPECS["trade_style"], sp,
                                     calibrated=True)
                if style["choice"] == "swing":
                    warnings.append(f"the request text reads as a swing trade (p={style['probabilities']['swing']}"
                                    f"); this decision is for the {req.interval} day-trade setup")
        decision_id = self.store.add_decision({
            "head": spec.name, "model": version, "model_version": (artifact or {}).get("model_version"),
            "symbol": symbol, "interval": req.interval, "time": f.get("time"), "session": session,
            "price": price, "pip": pip, "sl_pips": req.sl_pips, "tp_pips": req.tp_pips, "tp2_pips": req.tp2_pips,
            "action": action, "tier": d["tier"], "lean": side,
            "probabilities": {"buy": pb, "sell": ps, "hold": ph}, "explore": req.explore,
            "x": f.get("x"), "feature_version": f.get("feature_version"), "text": req.text})
        return {
            "decision_id": decision_id, "model": version, "head": spec.name, "symbol": symbol,
            "interval": req.interval, "time": f.get("time"), "session": session, "price": price, "pip": pip,
            "action": action, "tier": d["tier"], "lean": side,
            "probabilities": {"buy": round(pb, 6), "sell": round(ps, 6), "hold": round(ph, 6)},
            "calibrated": artifact is not None, "confidence": d["confidence"],
            "breakeven_probability": p0, "expected_r": d["expected_r"], "bernoulli": bernoulli,
            "entry": chosen["entry"], "stop": chosen["stop"], "targets": chosen["targets"], "levels": both,
            "sl_pips": req.sl_pips, "tp_pips": req.tp_pips, "tp2_pips": req.tp2_pips, "cost_pips": cost,
            "thresholds": cfg.to_dict(), "max_tier": max_tier, "exploration": bool(req.explore),
            "nouls": nouls, "not_applicable": off_pattern, "composite": composite,
            "reasons": reasons, "reason_groups": groups, "facts": f.get("facts"),
            "trade_style": style, "calibration": calibration, "data_note": f.get("data_note"),
            "warnings": warnings,
        }


def render_reasons(top: list[dict], cls: str, roles: dict, docs: Optional[dict]) -> list[dict]:
    out = []
    for a in top:
        role, _, short = a["feature"].partition("_")
        tf = roles.get(role)
        label = f"{short} [{tf}]" if tf else a["feature"]
        up = a["contribution"] > 0
        item = {**a, "timeframe": tf, "effect": f"{'raises' if up else 'lowers'} P({cls})",
                "text": f"{label} = {a['value']} {'raises' if up else 'lowers'} P({cls}) "
                        f"({a['contribution']:+.3f} log-odds)"}
        if docs and a["feature"] in docs:
            item["meaning"] = docs[a["feature"]]
        out.append(item)
    return out


def segment_summary(v: dict) -> dict:
    """One line per instrument (or year) of the holdout: the numbers, without the reliability tables."""
    pol = v.get("policy", {})
    keep = ("symbol", "pip", "spread_pips", "n_train", "n_holdout", "n", "observed_rate", "class_rates_holdout",
            "base_rates_train", "base_rate_train", "log_loss", "brier", "ece", "brier_skill_score",
            "directional_auc")
    out = {**{k: v.get(k) for k in keep if k in v},
           "baseline_log_loss": v.get("baseline", {}).get("log_loss"),
           "baseline_brier": v.get("baseline", {}).get("brier"),
           "has_skill": bool(v.get("skill", {}).get("has_skill")),
           "brier_gain_lower_95": v.get("skill", {}).get("lower_95")}
    if pol:
        out.update(policy_act=pol.get("holdout", {}).get("act"),
                   policy_act_or_confirm=pol.get("holdout", {}).get("act_or_confirm"),
                   p_value_act_or_confirm=pol.get("bernoulli", {}).get("act_or_confirm", {}).get("p_value"),
                   verdict=pol.get("verdict"))
    return out


def create_app(home: Optional[Path] = None, quant: Optional[QuantClient] = None,
               autotrain: Optional[bool] = None) -> FastAPI:
    svc = Service(home, quant)
    if autotrain is None:
        autotrain = os.environ.get("RLCD_AUTOTRAIN", "1") != "0"

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # The intent heads need no market data (their seed set ships with the service): train them on first
        # start so routing works out of the box. Setup heads are trained explicitly via /v1/train.
        if autotrain:
            have = svc.store.version(svc.store.latest_id()).get("artifacts", {})
            missing = [h for h in ("intent", "trade_style") if h not in have]
            if missing:
                try:
                    svc.trainer.start(TrainRequest(heads=missing))
                except Exception:
                    log.exception("autotrain of the intent heads failed to start")
        yield

    app = FastAPI(title="RLCD — Reinforcement Learning Calibrated Decision", version=VERSION, lifespan=lifespan,
                  description="Typed decisions with calibrated probabilities. Not financial advice.")
    app.state.svc = svc

    # ------------------------------------------------------------------ errors and auth
    @app.exception_handler(ContractError)
    async def _contract(_: Request, e: ContractError):
        return JSONResponse(e.body(), status_code=e.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, e: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in d.get("loc", []) if p != "body"), "message": d.get("msg")}
                   for d in e.errors()]
        msg = "; ".join(f"{d['field'] or 'body'}: {d['message']}" for d in details)
        return error(422, f"invalid request: {msg}", "validation", details=details)

    @app.exception_handler(QuantUnavailable)
    async def _quant_down(_: Request, e: QuantUnavailable):
        return error(503, str(e), "quant_unavailable")

    @app.exception_handler(QuantRejected)
    async def _quant_rejected(_: Request, e: QuantRejected):
        return error(422, str(e), "quant_rejected", quant_status=e.status)

    @app.exception_handler(Exception)
    async def _any(_: Request, e: Exception):
        log.exception("unhandled")
        return error(500, f"{type(e).__name__}: {str(e)[:400]}", "internal")

    @app.middleware("http")
    async def _auth(request: Request, call_next):
        key = os.environ.get("RLCD_API_KEY")
        if key and request.url.path != "/health":
            got = request.headers.get("x-api-key") or request.headers.get("authorization", "")[7:]
            if not hmac.compare_digest(got.encode(), key.encode()):
                return error(401, "missing or wrong API key (send `Authorization: Bearer <key>`)", "unauthorized")
        return await call_next(request)

    # ------------------------------------------------------------------ meta
    @app.get("/health")
    def health():
        version = svc.store.latest_id()
        have = svc.store.version(version).get("artifacts", {})
        running = svc.trainer.running()
        return {"status": "ok", "service": "rlcd", "version": VERSION, "model": version, "alias": LATEST,
                "heads": {n: {"trained": s.artifact in have} for n, s in svc.specs(version).items()},
                "quant": {"url": svc.quant.url, "reachable": svc.quant.reachable()},
                "training": running["job_id"] if running else None, "home": str(svc.store.home)}

    @app.get("/v1/models")
    def models():
        reg = svc.store.registry()
        latest = svc.store.latest_id()
        data = [{"id": LATEST, "alias_of": latest}]
        if not reg["versions"]:
            data.append({"id": latest, "trained_at": None, "heads": {},
                         "note": "untrained: every answer is the uninformed prior until POST /v1/train"})
        for vid in sorted(reg["versions"], key=lambda v: int(v.rsplit(".", 1)[1]), reverse=True):
            v = reg["versions"][vid]
            data.append({"id": vid, "trained_at": v.get("trained_at"), "retrained": v.get("retrained", []),
                         "heads": {n: {"trained": s.artifact in v["heads"],
                                       **({k: v["heads"][s.artifact].get(k)
                                           for k in ("n_train", "trained_at", "model_version", "metrics")}
                                          if s.artifact in v["heads"] else {})}
                                   for n, s in svc.specs(vid).items()}})
        return {"data": data}

    @app.get("/v1/heads")
    def heads(model: Optional[str] = None):
        version = svc.resolve(model)
        pending = svc.store.feedback_counts()
        return {"model": version,
                "heads": [svc.head_view(version, s, pending) for s in svc.specs(version).values()]}

    # ------------------------------------------------------------------ the evaluation endpoint
    @app.post("/v1/systemone")
    def systemone(req: SystemOneRequest):
        t0 = time.perf_counter()
        version = svc.resolve(req.model)
        questions = parse_questions(req.questions)
        memo, warnings = {}, []
        # speculative fan-out: one state, every question answered independently by its own head
        answers = {q.id: svc.answer(version, q, req.state, memo, warnings) for q in questions}
        out = {"model": version, "answers": answers,
               "usage": {"input_tokens": 0, "output_tokens": 0, "questions": len(questions),
                         "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}}
        if warnings:
            out["warnings"] = warnings
        return out

    @app.post("/v1/rank")
    def rank(req: RankRequest):
        t0 = time.perf_counter()
        version = svc.resolve(req.model)
        if not req.candidates:
            raise ContractError("candidates must be a non-empty array of {id, state}")
        if len(req.candidates) > MAX_CANDIDATES:
            raise ContractError(f"at most {MAX_CANDIDATES} candidates per call", limit=MAX_CANDIDATES)
        q = Question("question", req.question)
        option = req.option or q.option
        memo, warnings, ranked = {}, [], []
        for c in req.candidates:
            state = c.state
            if isinstance(req.state, dict) and isinstance(c.state, dict):
                state = {**req.state, **c.state}   # shared state, overridden per candidate
            elif c.state is None:
                state = req.state
            ans = svc.answer(version, q, state, memo, warnings)
            ranked.append({"id": c.id, "probability": positive_probability(ans, option), "answer": ans})
        # stable: ties keep the request order; a candidate the head does not apply to goes last
        ranked.sort(key=lambda r: -1.0 if r["probability"] is None else r["probability"], reverse=True)
        for i, r in enumerate(ranked):
            r["rank"] = i + 1
        out = {"model": version, "head": q.head, "option": option if q.type == "choice" else None,
               "candidates": ranked,
               "usage": {"input_tokens": 0, "output_tokens": 0, "questions": len(ranked),
                         "latency_ms": round((time.perf_counter() - t0) * 1000, 2)}}
        if warnings:
            out["warnings"] = warnings
        return out

    # ------------------------------------------------------------------ decisions
    @app.post("/v1/decide")
    def decide(req: DecideRequest):
        return svc.decide(req)

    @app.post("/v1/scan")
    def scan(req: ScanRequest):
        """The decision for every symbol, best expected R first: "every session, the major pairs"."""
        if req.symbols:
            symbols = list(dict.fromkeys(s.upper() for s in req.symbols))
        else:
            offered = {str(s.get("id")) for s in svc.quant.symbols()}
            symbols = [s for s in MAJORS if s in offered]
        if not symbols:
            raise ContractError("no symbols to scan")
        if len(symbols) > 40:
            raise ContractError("at most 40 symbols per scan", limit=40)
        pips = {k.upper(): v for k, v in (req.pip or {}).items()}
        results, errors = [], []
        for symbol in symbols:
            try:
                d = svc.decide(DecideRequest(model=req.model, symbol=symbol, interval=req.interval,
                                             as_of=req.as_of, pip=pips.get(symbol), sl_pips=req.sl_pips,
                                             tp_pips=req.tp_pips, tp2_pips=req.tp2_pips, explore=req.explore))
            except (QuantRejected, ContractError) as e:
                errors.append({"symbol": symbol, "error": str(e)})
                continue
            results.append({"symbol": d["symbol"], "session": d["session"], "action": d["action"],
                            "tier": d["tier"], "lean": d["lean"],
                            "best_expected_r": max(d["expected_r"].values()),
                            "p_above_breakeven": d["bernoulli"]["posterior"]["p_above_breakeven"],
                            "composite": {"buy": d["composite"]["buy"], "sell": d["composite"]["sell"]},
                            "decision": d})
        results.sort(key=lambda r: (r["best_expected_r"], r["p_above_breakeven"]), reverse=True)
        for i, r in enumerate(results):
            r["rank"] = i + 1
        return {"model": svc.resolve(req.model), "interval": req.interval,
                "ranked_by": "expected R of the best action, then P(true win rate > breakeven)",
                "sessions": sorted({r["session"] for r in results}), "results": results, "errors": errors}

    # ------------------------------------------------------------------ the reinforcement loop
    @app.post("/v1/feedback")
    def feedback(req: FeedbackRequest):
        version = svc.store.latest_id()
        row: dict = {"decision_id": req.decision_id, "weight": req.weight, "source": req.source}
        head, decision = req.head, None
        if req.decision_id:
            decision = svc.store.decision(req.decision_id)
            if decision is None:
                raise ContractError(f"unknown decision_id {req.decision_id!r}", kind="unknown_decision")
            head = head or decision["head"]
        if not head:
            raise ContractError("head is required (or a decision_id, which implies it)")
        spec = svc.spec(head, version)
        if spec.kind == "pattern":
            raise ContractError(f"feedback for pattern heads is not supported yet; '{head}' learns from the "
                                f"sidecar's labels at the next train", head=head)
        if not spec.trainable:
            raise ContractError(f"'{head}' is derived from '{spec.artifact}': post the outcome to "
                                f"'{spec.artifact}' with label buy, sell or hold", head=head)
        if req.label not in spec.classes:
            raise ContractError(f"label must be one of {list(spec.classes)} for head '{head}', got "
                                f"{req.label!r}", head=head, classes=list(spec.classes))
        row.update(head=head, label=req.label)
        update = None
        if spec.kind == "intent":
            if req.state is None:
                raise ContractError("state is required: the message (string or {text, attachments, timeframes, "
                                    "symbol}) the label belongs to")
            row["state"] = intent.normalize_state(req.state)
        else:
            state = req.state if isinstance(req.state, dict) else {}
            x = req.x if req.x is not None else state.get("x")
            src = {"x": x, "feature_version": req.feature_version or state.get("feature_version"),
                   "symbol": state.get("symbol"), "pip": state.get("pip"),
                   "time": state.get("time") or state.get("as_of")}
            scored = decision is not None and decision.get("head") == head
            if x is None and scored:
                src = {k: decision.get(k) for k in ("x", "feature_version", "symbol", "pip", "time")}
            elif x is None and state.get("symbol"):
                f = svc.features(None, spec, state, {})
                src = {k: f.get(k) for k in ("x", "feature_version", "symbol", "pip", "time")}
            if not isinstance(src["x"], list) or not src["feature_version"]:
                raise ContractError("a setup outcome needs the features it was scored on: a decision_id, or "
                                    "x + feature_version, or state {symbol, as_of} to fetch them")
            row.update(src)
            if scored:
                # The reinforcement step: the outcome updates the Beta-Bernoulli counts now, with no retrain.
                table = svc.store.ledger.table(head)
                if decision.get("model_version") and decision["model_version"] == table.get("model_version"):
                    cells = svc.store.ledger.update(head, decision, req.label)
                    update = ({"updated": True, "cells": cells} if cells is not None else
                              {"updated": False, "reason": "this decision_id was already scored"})
                else:
                    update = {"updated": False, "reason": "the decision was made by a model version other than "
                                                          "the one the posterior table belongs to"}
        saved = svc.store.add_feedback(row)
        return {"id": saved["id"], "stored": True, "head": head, "label": req.label,
                "feedback_rows": svc.store.feedback_counts().get(head, 0), "bernoulli": update,
                "note": "used as an up-weighted training row at the next POST /v1/train"}

    # ------------------------------------------------------------------ training
    @app.post("/v1/train")
    def train(req: Optional[TrainRequest] = None):
        try:
            job = svc.trainer.start(req or TrainRequest())
        except Busy as e:
            return error(409, f"training job {e.job_id} is still running", "busy", job_id=e.job_id)
        except TrainError as e:
            raise ContractError(str(e))
        return JSONResponse({"job_id": job["job_id"], "status": job["status"], "heads": list(job["heads"]),
                             "poll": f"/v1/train/{job['job_id']}"}, status_code=202)

    @app.get("/v1/train/{job_id}")
    def train_status(job_id: str):
        job = svc.trainer.get(job_id)
        if job is None:
            return error(404, f"unknown training job {job_id!r} (jobs are kept in memory until restart)",
                         "unknown_job")
        return job

    # ------------------------------------------------------------------ reports
    def trained(head: str, model: Optional[str]):
        version = svc.resolve(model)
        spec = svc.spec(head, version)
        artifact = svc.store.artifact(version, spec.artifact)
        if artifact is None:
            raise ContractError(f"head '{head}' is not trained in {version}: there is no holdout report yet",
                                kind="untrained", head=head)
        return version, spec, artifact

    @app.get("/v1/calibration")
    def calibration(head: str, model: Optional[str] = None, symbol: Optional[str] = None,
                    pip: Optional[float] = None, year: Optional[str] = None):
        version, spec, artifact = trained(head, model)
        rep = artifact["report"]
        meta = {"head": head, "type": spec.type, "model": version, "model_version": artifact.get("model_version"),
                "trained_at": artifact.get("trained_at"), "n_train": artifact.get("n_train"),
                "n_feedback": artifact.get("n_feedback", 0), "scope": "pooled holdout",
                "primary_metrics": ["log_loss", "brier"]}
        segs, years = rep.get("by_segment", {}), rep.get("by_year", {})
        if spec.kind != "intent":
            meta.update(calibration=artifact.get("calibration"), split=artifact.get("split"),
                        params=artifact.get("params"), feature_version=artifact.get("feature_version"),
                        notes=artifact.get("notes", []))
        if symbol:
            keys = [k for k, v in segs.items() if v["symbol"] == symbol.upper()
                    and (pip is None or abs(v["pip"] - pip) < 1e-12)]
            if len(keys) != 1:
                raise ContractError(f"no single holdout segment for symbol={symbol} pip={pip}",
                                    segments=sorted(segs))
            rep, meta["scope"] = segs[keys[0]], f"holdout rows of {keys[0]}"
        elif year:
            if year not in years:
                raise ContractError(f"no holdout rows in {year}", years=sorted(years))
            rep, meta["scope"] = years[year], f"holdout rows of {year}"
        if spec.type == "noul" and spec.kind == "setup":   # one-vs-rest view of the setup model's class
            c = spec.positive
            return {**meta, "positive": c, **rep["per_class"][c], "reliability": rep["reliability"]["per_class"][c],
                    "by_segment": {k: v["per_class"][c] for k, v in rep.get("by_segment", {}).items()},
                    "by_year": {k: v["per_class"][c] for k, v in rep.get("by_year", {}).items()}}
        out = {**meta, **{k: v for k, v in rep.items() if k not in ("by_segment", "by_year")}}
        if "by_segment" in rep:
            out["by_segment"] = {k: segment_summary(v) for k, v in rep["by_segment"].items()}
            out["by_year"] = {k: segment_summary(v) for k, v in rep.get("by_year", {}).items()}
        return out

    @app.get("/v1/bernoulli")
    def bernoulli(head: str, model: Optional[str] = None, cost_pips: Optional[float] = None):
        """The Beta-Bernoulli posterior table of a setup head and the binomial test of its holdout trades."""
        version, spec, artifact = trained(head, model)
        if spec.kind != "setup":
            raise ContractError(f"'{head}' has no trade outcomes; ask for a setup head", head=head)
        cfg = P.PolicyConfig.from_dict(artifact.get("policy"))
        params = artifact["params"]
        if cost_pips is None:
            spreads = sorted(s["spread_pips"] for s in artifact["segments"].values())
            cost_pips = spreads[len(spreads) // 2] if spreads else cfg.default_cost_pips
        p0 = float(P.breakeven(params["sl_pips"], params["tp_pips"], cost_pips))
        table = svc.store.ledger.table(spec.artifact)
        live = table.get("model_version") == artifact.get("model_version")
        pol = artifact["report"]["policy"]
        return {"head": spec.artifact, "model": version, "model_version": artifact.get("model_version"),
                "payoff_b": params["tp_pips"] / params["sl_pips"], "cost_pips": cost_pips,
                "breakeven": round(p0, 6), "config": cfg.bernoulli().to_dict(),
                "table_is_for_this_model": live,
                **B.table_view(table if live else B.Ledger.empty(), p0, cfg.bernoulli()),
                "binomial_test": pol.get("bernoulli"),
                "holdout_trades": {"act": pol["holdout"]["act"], "act_or_confirm": pol["holdout"]["act_or_confirm"]},
                "log_loss": artifact["metrics"].get("log_loss"),
                "baseline_log_loss": artifact["metrics"].get("baseline_log_loss"),
                "notes": ["bins: every prediction is a Bernoulli trial of its own probability bin; the counts "
                          "start from holdout rows spaced one label horizon apart and grow with each outcome "
                          "posted to /v1/feedback with a decision_id",
                          "symbol_session: signals (confirm or act) of that symbol in that session",
                          "breakeven here uses the median training spread; /v1/decide uses the instrument's own"]}

    return app


app = create_app()
