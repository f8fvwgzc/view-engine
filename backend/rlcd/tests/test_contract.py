import pytest

from rlcd.contract import Question, choice_answer, noul_answer, score_answer

CHOICE = {"type": "choice", "instructions": "what is the user asking for?",
          "criteria": {"position": "wants a trade", "research": "wants research", "other": "anything else"}}


def test_noul_shape():
    a = noul_answer(0.8, head="long_tp1:15m", calibrated=True)
    assert a == {"type": "noul", "noul": 0.8, "confidence": pytest.approx(0.6), "head": "long_tp1:15m",
                 "calibrated": True}


def test_choice_shape():
    a = choice_answer(["buy", "sell", "hold"], [0.6, 0.3, 0.1], head="setup:15m", calibrated=True)
    assert a["type"] == "choice" and a["choice"] == "buy"
    assert a["probabilities"] == {"buy": 0.6, "sell": 0.3, "hold": 0.1}
    assert a["confidence"] == pytest.approx(0.4) and a["head"] == "setup:15m" and a["calibrated"] is True


def test_score_shape():
    a = score_answer(["weak", "fair", "strong"], [0, 0.5, 0.5], head="h", calibrated=True)
    assert a["type"] == "score" and a["score"] == 1
    assert a["legend"] == {"0": "weak", "1": "fair", "2": "strong"}
    assert a["probabilities"] == {"0": 0.0, "1": 0.5, "2": 0.5}
    assert a["confidence"] == pytest.approx(0.25)


def test_systemone_answers_untrained_heads_with_the_prior(client):
    r = client.post("/v1/systemone", json={
        "model": "rlcd-latest", "state": "long or short now?",
        "questions": {"intent": CHOICE, "style": {"type": "choice", "head": "trade_style"},
                      "long": {"type": "noul", "head": "long_tp1:15m",
                               "criteria": {"true": "target first", "false": "stop first"}}}})
    assert r.status_code == 422      # the noul head needs a feature state, not a string
    r = client.post("/v1/systemone", json={"state": "long or short now?",
                                           "questions": {"intent": CHOICE, "s": {"type": "choice",
                                                                               "head": "trade_style"}}})
    body = r.json()
    assert r.status_code == 200 and body["model"] == "rlcd-0.1.0"
    assert set(body) == {"model", "answers", "usage", "warnings"}
    assert body["usage"]["input_tokens"] == 0 and body["usage"]["questions"] == 2
    a = body["answers"]["intent"]
    assert a["calibrated"] is False and a["confidence"] == 0 and a["head"] == "intent"
    assert a["probabilities"] == {k: pytest.approx(1 / 3, abs=1e-6) for k in ("position", "research", "other")}
    assert body["answers"]["s"]["probabilities"] == {"daytrade": 0.5, "swing": 0.5}
    assert "not trained" in body["warnings"][0]


def test_unknown_head_is_422_and_lists_heads(client):
    r = client.post("/v1/systemone", json={"state": "hi", "questions": {"is_spam": {"type": "noul"}}})
    assert r.status_code == 422
    body = r.json()
    assert body["kind"] == "unknown_head" and "intent" in body["available_heads"]
    assert "setup:15m" in body["available_heads"] and "setup:5m" in body["available_heads"]


def test_choice_limit_255_options(client):
    many = {f"o{i}": "x" for i in range(256)}
    r = client.post("/v1/systemone", json={"state": "hi", "questions": {"intent": {"type": "choice",
                                                                                "criteria": many}}})
    assert r.status_code == 422 and "255" in r.json()["error"]
    Question("q", {"type": "choice", "criteria": {f"o{i}": "x" for i in range(255)}})   # 255 itself is allowed


@pytest.mark.parametrize("levels", [1, 11])
def test_score_limit_2_to_10_levels(client, levels):
    q = {"type": "score", "head": "intent", "criteria": [f"level {i}" for i in range(levels)]}
    r = client.post("/v1/systemone", json={"state": "hi", "questions": {"q": q}})
    assert r.status_code == 422 and "2 to 10" in r.json()["error"]


def test_score_question_on_a_choice_head_is_rejected(client):
    q = {"type": "score", "head": "intent", "criteria": ["low", "mid", "high"]}
    r = client.post("/v1/systemone", json={"state": "hi", "questions": {"q": q}})
    assert r.status_code == 422 and "answers a choice question" in r.json()["error"]


def test_criteria_must_match_head_classes(client):
    q = {"type": "choice", "head": "intent", "criteria": {"position": "a", "spam": "b"}}
    r = client.post("/v1/systemone", json={"state": "hi", "questions": {"q": q}})
    assert r.status_code == 422 and r.json()["classes"] == ["position", "research", "other"]


@pytest.mark.parametrize("body", [{}, {"state": "hi"}, {"state": "hi", "questions": {}},
                                  {"state": "hi", "questions": {"intent": {"type": "essay"}}},
                                  {"state": "hi", "questions": {"intent": "choice"}}])
def test_malformed_requests_are_422_with_a_body(client, body):
    r = client.post("/v1/systemone", json=body)
    assert r.status_code == 422 and r.json()["error"] and r.json()["kind"] == "validation"


def test_unknown_model_is_422(client):
    r = client.post("/v1/systemone", json={"model": "rlcd-9.9.9", "state": "hi", "questions": {"intent": CHOICE}})
    assert r.status_code == 422 and r.json()["available"] == ["rlcd-latest"]


def test_health_models_heads_untrained(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["model"] == "rlcd-0.1.0" and h["quant"]["reachable"] is False
    assert h["heads"]["intent"] == {"trained": False}
    m = client.get("/v1/models").json()["data"]
    assert m[0] == {"id": "rlcd-latest", "alias_of": "rlcd-0.1.0"} and m[1]["trained_at"] is None
    heads = {h["name"]: h for h in client.get("/v1/heads").json()["heads"]}
    assert heads["intent"]["type"] == "choice" and heads["intent"]["classes"] == ["position", "research", "other"]
    assert heads["long_tp1:1h"]["type"] == "noul" and heads["long_tp1:1h"]["backed_by"] == "setup:1h"
    assert heads["setup:15m"]["classes"] == ["buy", "sell", "hold"] and not heads["setup:15m"]["trained"]


def test_quant_unreachable_is_503_and_untrained_decision_needs_it(client):
    r = client.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "15m"})
    assert r.status_code == 503 and r.json()["kind"] == "quant_unavailable"
    assert client.post("/v1/decide", json={"symbol": "XAUUSD", "interval": "2h"}).status_code == 422
    assert client.get("/v1/calibration", params={"head": "intent"}).status_code == 422


def test_api_key_when_configured(client, monkeypatch):
    monkeypatch.setenv("RLCD_API_KEY", "s3cret")
    assert client.get("/v1/heads").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/v1/heads", headers={"Authorization": "Bearer s3cret"}).status_code == 200
