"""Head registry.

A head is a trained, calibrated answerer for one fixed question. Unlike the reference there is no general
language model: a question is answered only if it is bound to a head (question field `head`, or the question
id). Several heads can share one trained artifact: `long_tp1:15m` and `short_tp1:15m` are the buy and sell
probabilities of the `setup:15m` model, exposed as nouls.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

INTERVALS = ("5m", "15m", "1h")
SETUP_CLASSES = ("buy", "sell", "hold")

INTENT_INPUT = {
    "state": "a string (the message) or an object with any subset of the fields below",
    "text": "string: the user's message",
    "attachments": "int: number of attached screenshots/files (a list is counted)",
    "timeframes": "array of strings such as M15, 1h, D1",
    "symbol": "string or null: the instrument already detected by the caller",
}
SETUP_INPUT = {
    "state": "either a feature vector or an instrument to fetch features for",
    "feature vector": {"x": "array of numbers (null = missing)", "feature_names": "array of strings",
                       "feature_version": "string; must equal the version the head was trained on"},
    "instrument": {"symbol": "string", "interval": "optional; must equal the head's interval",
                   "as_of": "optional UTC ISO timestamp (replay)", "pip": "optional pip size"},
}


@dataclass(frozen=True)
class HeadSpec:
    name: str
    type: str                      # noul | choice | score
    classes: tuple[str, ...]       # choice options / score levels in order; empty for a noul
    artifact: str                  # key of the trained artifact backing this head
    kind: str                      # intent | setup
    description: str
    input: dict
    positive: Optional[str] = None  # noul: the class of the backing model whose probability is the answer
    trainable: bool = True          # False for heads derived from another head's model
    interval: Optional[str] = None


def _specs() -> dict[str, HeadSpec]:
    out = [
        HeadSpec("intent", "choice", ("position", "research", "other"), "intent", "intent",
                 "Is the user asking for a trade position, for research, or for something else?", INTENT_INPUT),
        HeadSpec("trade_style", "choice", ("daytrade", "swing"), "trade_style", "intent",
                 "Which trading style does a position request imply? Trained on position requests only: "
                 "gate on intent=position first.", INTENT_INPUT),
    ]
    for iv in INTERVALS:
        out.append(HeadSpec(f"setup:{iv}", "choice", SETUP_CLASSES, f"setup:{iv}", "setup",
                            f"From this {iv} candle's close, which action reaches its target before its stop "
                            f"within the horizon: buy, sell, or neither (hold)?", SETUP_INPUT, interval=iv))
        out.append(HeadSpec(f"long_tp1:{iv}", "noul", (), f"setup:{iv}", "setup",
                            f"Does a long from this {iv} close reach target 1 before its stop? "
                            f"(P(buy) of setup:{iv})", SETUP_INPUT, positive="buy", trainable=False, interval=iv))
        out.append(HeadSpec(f"short_tp1:{iv}", "noul", (), f"setup:{iv}", "setup",
                            f"Does a short from this {iv} close reach target 1 before its stop? "
                            f"(P(sell) of setup:{iv})", SETUP_INPUT, positive="sell", trainable=False,
                            interval=iv))
    return {s.name: s for s in out}


SPECS: dict[str, HeadSpec] = _specs()
# outcome labels that already have a head derived from the setup model
DERIVED_LABELS = ("long_tp1", "short_tp1")


def pattern_spec(label: str, interval: str, doc: str = "") -> HeadSpec:
    """A yes/no head for one outcome label the sidecar exports (discovered from the data, not hard-coded).
    It only applies in the chart situation the label is defined for; elsewhere the answer is
    `applicable: false` with `noul: null`."""
    name = f"{label}:{interval}"
    return HeadSpec(name, "noul", (), name, "pattern", doc or f"Outcome label '{label}' on the {interval} chart.",
                    SETUP_INPUT, positive=label, interval=interval)


def spec_from_registry(name: str, info: dict) -> Optional[HeadSpec]:
    s = (info or {}).get("spec")
    return pattern_spec(s["label"], s["interval"], s.get("doc", "")) if s else None


def available() -> list[str]:
    return list(SPECS)


def trainable() -> list[str]:
    return [n for n, s in SPECS.items() if s.trainable]
