from dataclasses import dataclass

from kalshi_engine import topics
from kalshi_engine.topics import CULTURAL, ECONOMIC, GEOPOLITICAL, Subject, TopicClassifier, decide, rule_topic


def S(key="kalshi:E1", venue="kalshi", title="Event", hint=None, sample=None):
    return Subject(key=key, venue=venue, title=title, hint=hint, sample=sample or ["a", "b"])


@dataclass
class FakeResult:
    probs: dict
    route: str = "typesafe"


class FakeJev:
    def __init__(self, probs=None, route="typesafe"):
        self.calls = []
        self.probs = probs or {CULTURAL: 0.1, ECONOMIC: 0.9, GEOPOLITICAL: 0.05}
        self.route = route

    def __call__(self, state, questions):
        self.calls.append(state)
        assert set(questions) == {CULTURAL, ECONOMIC, GEOPOLITICAL}
        return FakeResult(self.probs, self.route)


# --- rules for clear-cut categories -----------------------------------------------

def test_clear_kalshi_categories_need_no_jev():
    assert rule_topic(S(hint="Economics")) == (True, ECONOMIC)
    assert rule_topic(S(hint="Entertainment")) == (True, CULTURAL)
    assert rule_topic(S(hint="Sports")) == (True, None)
    assert rule_topic(S(hint="Elections")) == (True, None)


def test_grey_kalshi_categories_go_to_jev():
    for hint in ("Politics", "Science and Technology", "Crypto", "World", None):
        assert rule_topic(S(hint=hint)) == (False, None)


def test_polymarket_fee_schedule_is_the_hint():
    assert rule_topic(S(venue="polymarket", hint="culture_fees")) == (True, CULTURAL)
    assert rule_topic(S(venue="polymarket", hint="finance_prices_fees")) == (True, ECONOMIC)
    assert rule_topic(S(venue="polymarket", hint="sports_fees_v3")) == (True, None)
    assert rule_topic(S(venue="polymarket", hint="zero_fees")) == (False, None)
    assert rule_topic(S(venue="polymarket", hint="crypto_fees_v2")) == (False, None)


def test_war_is_geopolitical():
    assert decide({CULTURAL: 0.02, ECONOMIC: 0.05, GEOPOLITICAL: 0.93}) == GEOPOLITICAL


def test_new_question_version_changes_the_cache_key(monkeypatch):
    subj = S(hint="World", title="Will Russia enter the town?")
    before = subj.text_hash
    monkeypatch.setattr(topics, "QUESTIONS_VERSION", topics.QUESTIONS_VERSION + 1)
    assert subj.text_hash != before  # old two-topic verdicts get re-judged


def test_decide_needs_a_confident_answer():
    assert decide({CULTURAL: 0.2, ECONOMIC: 0.85}) == ECONOMIC
    assert decide({CULTURAL: 0.7, ECONOMIC: 0.3}) == CULTURAL
    assert decide({CULTURAL: 0.4, ECONOMIC: 0.5}) is None


# --- the Jev-backed classifier ---------------------------------------------------------

def test_grey_event_asked_once_then_cached(tmp_path):
    jev = FakeJev()
    subj = S(hint="Politics", title="Will the Fed chair be confirmed?")
    first = TopicClassifier(tmp_path / "t.jsonl", ask=jev).classify([subj])
    assert first == {subj.key: ECONOMIC} and len(jev.calls) == 1

    again = TopicClassifier(tmp_path / "t.jsonl", ask=jev)
    assert again.classify([subj]) == {subj.key: ECONOMIC}
    assert len(jev.calls) == 1 and again.stats["cached"] == 1


def test_reworded_event_is_asked_again(tmp_path):
    jev = FakeJev()
    TopicClassifier(tmp_path / "t.jsonl", ask=jev).classify([S(hint="Politics", title="old wording")])
    TopicClassifier(tmp_path / "t.jsonl", ask=jev).classify([S(hint="Politics", title="new wording")])
    assert len(jev.calls) == 2


def test_rule_decided_events_never_call_jev(tmp_path):
    jev = FakeJev()
    out = TopicClassifier(tmp_path / "t.jsonl", ask=jev).classify([S(hint="Economics"), S(key="kalshi:E2", hint="Sports")])
    assert out == {"kalshi:E1": ECONOMIC, "kalshi:E2": None} and jev.calls == []


def test_budget_defers_the_rest_to_a_later_run(tmp_path):
    jev = FakeJev()
    subjects = [S(key=f"kalshi:E{i}", hint="Politics", title=f"event {i}") for i in range(5)]
    clf = TopicClassifier(tmp_path / "t.jsonl", ask=jev, max_new=2)
    out = clf.classify(subjects)
    assert len(jev.calls) == 2 and clf.stats["deferred"] == 3
    assert sum(1 for v in out.values() if v) == 2  # deferred ones are out for now, not guessed


def test_mock_answers_are_neither_trusted_nor_cached(tmp_path):
    jev = FakeJev(probs={CULTURAL: 0.99, ECONOMIC: 0.0}, route="mock")
    subj = S(hint="Politics")
    assert TopicClassifier(tmp_path / "t.jsonl", ask=jev).classify([subj]) == {subj.key: None}
    assert not (tmp_path / "t.jsonl").exists()


def test_subject_text_carries_title_hint_and_examples():
    text = S(hint="Politics", title="Tariff decision", sample=["Yes by Oct", "No"]).text
    assert "Tariff decision" in text and "Politics" in text and "Yes by Oct" in text
    assert topics.TOPIC_THRESHOLD > 0.5
