"""Report memory: exact plan reuse, similar examples, outcome and feedback scoring."""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.report_memory.embed import Embedder
from app.modules.report_memory.store import MemoryStore

SCOPE = "shipment_api|all|manifest_datetime"
LANE_PLAN = {"report_title": "Lane Wise Shipments", "sections": [{"id": "by_lane", "dimensions": ["lane"]}]}


@pytest.fixture
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory.sqlite", Embedder("unused", semantic=False))
    yield s
    s.close()


def test_a_successful_question_is_reused_exactly(store):
    store.record("Lane wise shipments", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)
    # Case, punctuation and spacing do not make a different question.
    assert store.lookup("lane-wise   SHIPMENTS!", SCOPE)["cached_plan"] == LANE_PLAN
    # Another scope shape (e.g. channel orders) never reuses it.
    assert store.lookup("lane wise shipments", "channel_api|all|order_date")["cached_plan"] is None


def test_plans_with_dates_are_examples_but_never_reused_exactly(store):
    store.record("lane wise shipments from 1 to 10 oct", SCOPE, LANE_PLAN, "r1", "generated", cacheable=False)
    result = store.lookup("lane wise shipments from 1 to 10 oct", SCOPE)
    assert result["cached_plan"] is None


def test_similar_questions_come_back_as_examples(store):
    store.record("lane wise shipments", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)
    store.record("courier wise delivery rate", SCOPE, {"sections": [{"dimensions": ["courier"]}]}, "r2", "generated", True)
    examples = store.lookup("lane wise cancelled shipments", SCOPE)["examples"]
    assert [e["prompt"] for e in examples] == ["lane wise shipments"]
    assert examples[0]["plan"] == LANE_PLAN


def test_failures_and_dislikes_stop_reuse_and_likes_restore_it(store):
    store.record("lane wise shipments", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)
    assert store.feedback("r1", "down")
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] is None
    store.feedback("r1", "up")        # replaces the dislike: 1 + 3
    store.feedback("r1", "up")        # counted once per report
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] == LANE_PLAN
    assert store.stats()["reusable"] == 1
    store.record("lane wise shipments", SCOPE, LANE_PLAN, "r2", "failed", cacheable=True)
    store.record("lane wise shipments", SCOPE, LANE_PLAN, "r3", "failed", cacheable=True)
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] is None
    assert not store.feedback("unknown-report", "up")


def test_star_rating_replaces_the_previous_verdict(store):
    store.record("lane wise shipments", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)   # score 1
    store.feedback("r1", "rating:1")                                                          # 1 - 5
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] is None
    store.feedback("r1", "rating:5")                                                          # 1 + 3, not 1 - 5 + 3
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] == LANE_PLAN
    store.feedback("r1", "down")                                                              # replaces the 5 stars
    assert store.lookup("lane wise shipments", SCOPE)["cached_plan"] is None
    store.feedback("r1", "rating:4")
    assert store.stats()["ratings"] == 1 and store.stats()["average_rating"] == 4


def test_memory_persists_across_restarts(tmp_path):
    first = MemoryStore(tmp_path / "m.sqlite", Embedder("unused", semantic=False))
    first.record("lane wise shipments", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)
    first.close()
    second = MemoryStore(tmp_path / "m.sqlite", Embedder("unused", semantic=False))
    assert second.lookup("lane wise shipments", SCOPE)["cached_plan"] == LANE_PLAN
    assert second.lookup("lane wise cancelled shipments", SCOPE)["examples"]
    second.close()


def test_api_round_trip():
    with TestClient(create_app()) as client:
        body = {"prompt": "pickup pincode wise orders", "scope_key": SCOPE, "plan": LANE_PLAN,
                "report_id": "api-r1", "outcome": "generated", "cacheable": True}
        assert client.post("/api/report_memory/record", json=body).status_code == 200
        found = client.post("/api/report_memory/lookup", json={"prompt": "Pickup pincode wise orders", "scope_key": SCOPE})
        assert found.json()["cached_plan"] == LANE_PLAN
        assert client.post("/api/report_memory/feedback", json={"report_id": "api-r1", "signal": "saved"}).json() == {"applied": True}
        assert client.post("/api/report_memory/feedback", json={"report_id": "api-r1", "signal": "bogus"}).status_code == 422
        assert client.get("/api/report_memory/stats").json()["questions"] >= 1


def test_hindi_question_finds_the_english_one_with_the_multilingual_model(tmp_path):
    embedder = Embedder("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2", semantic=True)
    if embedder.semantic_vector("test") is None:
        pytest.skip("multilingual model not installed or not downloadable here")
    store = MemoryStore(tmp_path / "m.sqlite", embedder)
    store.record("lane wise shipment report", SCOPE, LANE_PLAN, "r1", "generated", cacheable=True)
    store.record("COD vs prepaid payment breakdown", SCOPE, {"sections": [{"dimensions": ["payment_mode"]}]},
                 "r2", "generated", True)
    examples = store.lookup("लेन के हिसाब से शिपमेंट रिपोर्ट", SCOPE)["examples"]
    assert examples and examples[0]["prompt"] == "lane wise shipment report"
    store.close()
