import pytest

from rlcd.heads.intent import normalize_state, tf_minutes
from rlcd.seeds import intent as seeds
from tests.conftest import make_client, train

QS = {"intent": {"type": "choice"}, "trade_style": {"type": "choice"}}


def ask(client, state):
    r = client.post("/v1/systemone", json={"model": "rlcd-latest", "state": state, "questions": QS})
    assert r.status_code == 200, r.text
    return r.json()["answers"]


def test_seed_set_is_deterministic_and_large():
    a, b = seeds.generate(7), seeds.generate(7)
    assert a == b and a != seeds.generate(8)
    for cls in ("position", "research", "other"):
        assert sum(r["intent"] == cls for r in a) >= 400
    assert sum(r["style"] == "swing" for r in a) >= 300 and sum(r["style"] == "daytrade" for r in a) >= 300


def test_timeframe_parsing():
    assert [tf_minutes(x) for x in ("M15", "15m", "h1", "1 hour", "H4", "D1", "1w", "weekly", "gold")] == \
        [15, 15, 60, 60, 240, 1440, 10080, 10080, None]
    assert normalize_state("hi") == {"text": "hi", "attachments": 0, "timeframes": [], "symbol": None}
    assert normalize_state({"attachments": ["a.png", "b.png"]})["attachments"] == 2


@pytest.mark.parametrize("text,intent,style", [
    ("long or short now?", "position", "daytrade"),
    ("xauusd m15 where entry", "position", "daytrade"),
    ("what position can I open today", "position", "daytrade"),
    ("scalp eurusd m5 london session", "position", "daytrade"),
    ("is gold buy this week based on fed and cpi", "position", "swing"),
    ("should I hold gbpjpy long for a few days, d1 trend is up", "position", "swing"),
    ("research the best vector database", "research", None),
    ("compare postgres and sqlite for a small team", "research", None),
    ("hello", "other", None),
    ("fix the bug in the login page", "other", None),
])
def test_obvious_cases(intent_client, text, intent, style):
    a = ask(intent_client, {"text": text})
    assert a["intent"]["choice"] == intent and a["intent"]["calibrated"] is True
    assert a["intent"]["confidence"] > 0.5
    assert sum(a["intent"]["probabilities"].values()) == pytest.approx(1.0, abs=1e-4)
    if style:
        assert a["trade_style"]["choice"] == style


def test_screenshots_with_few_words_are_a_position_request(intent_client):
    a = ask(intent_client, {"text": "", "attachments": 2, "timeframes": ["M15"], "symbol": "XAUUSD"})
    assert a["intent"]["choice"] == "position" and a["trade_style"]["choice"] == "daytrade"
    a = ask(intent_client, {"text": "gold weekly", "attachments": 1, "timeframes": ["W1", "D1"]})
    assert a["intent"]["choice"] == "position" and a["trade_style"]["choice"] == "swing"


def test_holdout_metrics_are_reported(intent_client):
    for head in ("intent", "trade_style"):
        m = intent_client.job["heads"][head]["metrics"]
        assert m["accuracy"] > 0.8 and m["brier"] < m["baseline_brier"] and m["has_skill"]
        assert m["log_loss"] < m["baseline_log_loss"]
    cal = intent_client.get("/v1/calibration", params={"head": "intent"}).json()
    assert len(cal["reliability"]["top_label"]) == 10 and cal["brier_skill_score"] > 0.5
    assert set(cal["reliability"]["per_class"]) == {"position", "research", "other"}
    models = intent_client.get("/v1/models").json()["data"]
    assert models[0] == {"id": "rlcd-latest", "alias_of": "rlcd-0.1.1"}
    assert models[1]["heads"]["intent"]["metrics"]["n_holdout"] > 300


def test_feedback_rows_are_picked_up_by_the_next_train(tmp_path):
    c = make_client(tmp_path)
    for i in range(30):   # the user's own corrections of a phrasing the seeds do not contain
        r = c.post("/v1/feedback", json={"head": "intent", "state": {"text": f"zorblax quux brief {i}"},
                                         "label": "research", "source": "user_correction"})
        assert r.status_code == 200 and r.json()["feedback_rows"] == i + 1
    bad = c.post("/v1/feedback", json={"head": "intent", "state": "x", "label": "spam", "source": "t"})
    assert bad.status_code == 422
    job = train(c, {"heads": ["intent"]})
    assert job["status"] == "succeeded" and job["heads"]["intent"]["n_feedback"] == 30
    a = c.post("/v1/systemone", json={"state": "zorblax quux", "questions": {"intent": {"type": "choice"}}})
    assert a.json()["answers"]["intent"]["choice"] == "research"
    job2 = train(c, {"heads": ["intent"]})
    assert job2["model"] == "rlcd-0.1.2" and c.get("/health").json()["model"] == "rlcd-0.1.2"   # N increments
    # the older version stays addressable
    old = c.post("/v1/systemone", json={"model": "rlcd-0.1.1", "state": "hello",
                                        "questions": {"intent": {"type": "choice"}}})
    assert old.status_code == 200 and old.json()["model"] == "rlcd-0.1.1"


def test_rank_sorts_candidates_by_option_probability(intent_client):
    cands = [{"id": "greeting", "state": "hello there"}, {"id": "trade", "state": "buy or sell gold now m15"},
             {"id": "paper", "state": "research the best vector database"}]
    r = intent_client.post("/v1/rank", json={"model": "rlcd-latest", "candidates": cands, "option": "position",
                                             "question": {"type": "choice", "head": "intent"}})
    assert r.status_code == 200, r.text
    out = r.json()["candidates"]
    probs = [c["probability"] for c in out]
    assert out[0]["id"] == "trade" and probs == sorted(probs, reverse=True) and [c["rank"] for c in out] == [1, 2, 3]
    assert all(c["probability"] == c["answer"]["probabilities"]["position"] for c in out)
    r2 = intent_client.post("/v1/rank", json={"candidates": cands, "option": "research",
                                              "question": {"type": "choice", "head": "intent"}})
    assert r2.json()["candidates"][0]["id"] == "paper"
    r3 = intent_client.post("/v1/rank", json={"candidates": cands, "question": {"type": "choice", "head": "intent"}})
    assert r3.status_code == 422 and "option" in r3.json()["error"]
