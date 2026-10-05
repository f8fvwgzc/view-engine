"""The wire contract: request validation and typed answers.

Follows the reference evaluation contract (`{model, state, questions}` -> `{model, answers, usage}`) with
three question types: noul (probability of yes), choice (one of N options), score (ordered levels).

RLCD extensions, all additive:
  * question field `head` binds a question to a trained head (default: the question id);
  * every answer carries `head` and `calibrated`; noul answers also carry `confidence`;
  * `instructions` is accepted and ignored: there is no language model here, the head defines the question.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .confidence import choice_confidence, noul_confidence, score_confidence

QUESTION_TYPES = ("noul", "choice", "score")
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS, MAX_SCORE_LEVELS = 2, 10
MAX_QUESTIONS = 64
MAX_CANDIDATES = 200


class ContractError(Exception):
    """A request that cannot be answered as asked. Rendered as HTTP 422 unless `status` says otherwise."""

    def __init__(self, message: str, *, kind: str = "validation", status: int = 422, **details: Any):
        super().__init__(message)
        self.message, self.kind, self.status, self.details = message, kind, status, details

    def body(self) -> dict:
        return {"error": self.message, "kind": self.kind, **self.details}


# ----------------------------------------------------------------------------- request bodies

class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SystemOneRequest(_Body):
    model: Optional[str] = None
    state: Any = None
    questions: dict[str, Any]


class Candidate(_Body):
    id: str
    state: Any = None


class RankRequest(_Body):
    model: Optional[str] = None
    state: Any = None
    candidates: list[Candidate]
    question: dict[str, Any]
    option: Optional[str] = None


class DecideRequest(_Body):
    model: Optional[str] = None
    symbol: str
    interval: str = "15m"
    as_of: Optional[str] = None
    pip: Optional[float] = Field(None, gt=0)
    sl_pips: float = Field(20, gt=0)
    tp_pips: float = Field(50, gt=0)
    tp2_pips: float = Field(100, gt=0)
    cost_pips: Optional[float] = Field(None, ge=0)
    text: Optional[str] = None
    # Thompson sampling: let a draw from the win-rate posterior decide the Bernoulli gate. Off by default;
    # a decision made this way is labelled as exploration.
    explore: bool = False


class ScanRequest(_Body):
    model: Optional[str] = None
    symbols: Optional[list[str]] = None
    interval: str = "15m"
    as_of: Optional[str] = None
    sl_pips: float = Field(20, gt=0)
    tp_pips: float = Field(50, gt=0)
    tp2_pips: float = Field(100, gt=0)
    pip: Optional[dict[str, float]] = None
    explore: bool = False


class FeedbackRequest(_Body):
    decision_id: Optional[str] = None
    head: Optional[str] = None
    state: Any = None
    x: Optional[list[Optional[float]]] = None
    feature_names: Optional[list[str]] = None
    feature_version: Optional[str] = None
    label: Any = None
    weight: float = Field(1.0, gt=0, le=100)
    source: str = Field(..., min_length=1, max_length=120)


class TrainRequest(_Body):
    heads: Optional[list[str]] = None
    symbols: Optional[list[str]] = None
    intervals: Optional[list[str]] = None
    sl_pips: float = Field(20, gt=0)
    tp_pips: float = Field(50, gt=0)
    tp2_pips: float = Field(100, gt=0)
    horizon: int = Field(48, ge=1, le=2000)
    max_rows: int = Field(20000, ge=200, le=200000)   # JSON export only: rows per symbol
    # bulk training: `npz` = binary export without a row cap, `json` = capped JSON, `auto` = npz if offered
    format: str = Field("auto", pattern="^(auto|npz|json)$")
    start: Optional[str] = None
    end: Optional[str] = None
    # at most this many rows per head after pooling; reached by taking every k-th candle of each symbol
    max_train_rows: int = Field(600000, ge=1000, le=5000000)
    quant_url: Optional[str] = None
    # also train one yes/no head per extra outcome label found in the export (`meta.label_names`)
    patterns: bool = True
    # pip sizes to export per symbol (several = several training segments); others use the sidecar default.
    # Gold is trained on both conventions (0.1 and 1.0) because a "pip" of gold is not standardised.
    pips: Optional[dict[str, list[float]]] = None
    feedback_weight: float = Field(5.0, gt=0, le=1000)
    n_estimators: int = Field(300, ge=10, le=3000)
    seed: int = 7


# ----------------------------------------------------------------------------- questions

class Question:
    """A validated question. Structural limits are checked here; head compatibility in `bind`."""

    def __init__(self, qid: str, raw: Any):
        if not isinstance(raw, dict):
            raise ContractError(f"question '{qid}' must be an object", question=qid)
        qtype = raw.get("type")
        if qtype not in QUESTION_TYPES:
            raise ContractError(f"question '{qid}': type must be one of {list(QUESTION_TYPES)}, got {qtype!r}",
                                question=qid)
        instructions = raw.get("instructions", "")
        if instructions is not None and not isinstance(instructions, str):
            raise ContractError(f"question '{qid}': instructions must be a string", question=qid)
        head = raw.get("head", qid)
        if not isinstance(head, str) or not head.strip():
            raise ContractError(f"question '{qid}': head must be a non-empty string", question=qid)
        self.id, self.type, self.head = qid, qtype, head.strip()
        self.instructions = instructions or ""
        self.option: Optional[str] = raw.get("option") if isinstance(raw.get("option"), str) else None
        self.criteria = self._criteria(raw.get("criteria"))

    def _criteria(self, c: Any) -> Any:
        q = self.id
        if self.type == "noul":
            if c is None:
                return None
            if not isinstance(c, dict) or not set(map(str, c)) <= {"true", "false"}:
                raise ContractError(f"question '{q}': noul criteria is an optional object with keys 'true' and "
                                    f"'false'", question=q)
            return {str(k): v for k, v in c.items()}
        if self.type == "choice":
            if c is None:
                return None  # RLCD: the head already fixes the options
            if not isinstance(c, dict) or not c:
                raise ContractError(f"question '{q}': choice criteria must be a non-empty object mapping each "
                                    f"option to its description", question=q)
            if len(c) > MAX_CHOICE_OPTIONS:
                raise ContractError(f"question '{q}': a choice can have at most {MAX_CHOICE_OPTIONS} options, "
                                    f"got {len(c)}", question=q, limit=MAX_CHOICE_OPTIONS)
            return {str(k): v for k, v in c.items()}
        if c is None:
            return None
        if not isinstance(c, list):
            raise ContractError(f"question '{q}': score criteria must be an ordered array of level descriptions",
                                question=q)
        if not MIN_SCORE_LEVELS <= len(c) <= MAX_SCORE_LEVELS:
            raise ContractError(f"question '{q}': a score needs {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} levels, "
                                f"got {len(c)}", question=q, limit=[MIN_SCORE_LEVELS, MAX_SCORE_LEVELS])
        return [str(x) for x in c]

    def bind(self, spec: Any) -> None:
        """Check this question against the head it is bound to (`spec`: heads.base.HeadSpec)."""
        q = self.id
        if spec.type != self.type:
            raise ContractError(f"question '{q}': head '{spec.name}' answers a {spec.type} question, "
                                f"not a {self.type}", question=q, head=spec.name, head_type=spec.type)
        if self.type == "choice" and self.criteria is not None:
            want, got = set(spec.classes), set(self.criteria)
            if want != got:
                raise ContractError(
                    f"question '{q}': criteria options {sorted(got)} do not match head '{spec.name}' classes "
                    f"{list(spec.classes)}", question=q, head=spec.name, classes=list(spec.classes))
        if self.type == "score" and self.criteria is not None and len(self.criteria) != len(spec.classes):
            raise ContractError(
                f"question '{q}': head '{spec.name}' has {len(spec.classes)} levels, criteria has "
                f"{len(self.criteria)}", question=q, head=spec.name, levels=len(spec.classes))


def parse_questions(raw: Any) -> list[Question]:
    if not isinstance(raw, dict) or not raw:
        raise ContractError("questions must be a non-empty object mapping question id -> question")
    if len(raw) > MAX_QUESTIONS:
        raise ContractError(f"at most {MAX_QUESTIONS} questions per call, got {len(raw)}", limit=MAX_QUESTIONS)
    return [Question(str(qid), q) for qid, q in raw.items()]


# ----------------------------------------------------------------------------- answers

def _r(p: float) -> float:
    return round(float(p), 6)


def noul_answer(p: float, *, head: str, calibrated: bool) -> dict:
    p = min(1.0, max(0.0, float(p)))
    return {"type": "noul", "noul": _r(p), "confidence": _r(noul_confidence(p)), "head": head,
            "calibrated": calibrated}


def choice_answer(options: Sequence[str], probabilities: Sequence[float], *, head: str, calibrated: bool) -> dict:
    ps = [float(p) for p in probabilities]
    best = max(range(len(ps)), key=lambda i: (ps[i], -i))
    return {"type": "choice", "choice": options[best],
            "probabilities": {o: _r(p) for o, p in zip(options, ps)},
            "confidence": _r(choice_confidence(ps)), "head": head, "calibrated": calibrated}


def score_answer(levels: Sequence[str], probabilities: Sequence[float], *, head: str, calibrated: bool) -> dict:
    """Levels are indexed from 0 in the order of `criteria`; `legend` maps index -> description."""
    ps = [float(p) for p in probabilities]
    best = max(range(len(ps)), key=lambda i: (ps[i], -i))
    return {"type": "score", "score": best, "legend": {str(i): d for i, d in enumerate(levels)},
            "probabilities": {str(i): _r(p) for i, p in enumerate(ps)},
            "confidence": _r(score_confidence(ps)), "head": head, "calibrated": calibrated}


def build_answer(question: Question, spec: Any, probabilities: Sequence[float], calibrated: bool) -> dict:
    """`probabilities` are in the order of `spec.classes` (for a noul head: a single yes-probability)."""
    if spec.type == "noul":
        return noul_answer(probabilities[0], head=spec.name, calibrated=calibrated)
    if spec.type == "choice":
        return choice_answer(list(spec.classes), probabilities, head=spec.name, calibrated=calibrated)
    levels = question.criteria if question.criteria is not None else list(spec.classes)
    return score_answer(levels, probabilities, head=spec.name, calibrated=calibrated)


def positive_probability(answer: dict, option: Optional[str]) -> Optional[float]:
    """The probability /v1/rank sorts by: the yes-probability of a noul, or one named option of a choice.
    None when a pattern head does not apply to the candidate."""
    if answer["type"] == "noul":
        return None if answer["noul"] is None else float(answer["noul"])
    if answer["type"] == "choice":
        if option is None:
            raise ContractError("ranking by a choice question needs `option`: the option whose probability "
                                "is the sort key", options=list(answer["probabilities"]))
        if option not in answer["probabilities"]:
            raise ContractError(f"option {option!r} is not one of {list(answer['probabilities'])}",
                                options=list(answer["probabilities"]))
        return float(answer["probabilities"][option])
    raise ContractError("ranking needs a noul or a choice question")
