import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import label_relations as lr  # noqa: E402


def P(ab, ba, me, ex, su):
    return {"a_implies_b": ab, "b_implies_a": ba, "mutually_exclusive": me, "exhaustive": ex, "same_underlying": su}


def test_score_counts_right_wrong_and_missed():
    labels = [
        {"a": "1", "b": "1", "probs": P(.1, .85, .03, .07, .96), "truth": ["b_implies_a"]},  # confirmed, right
        {"a": "2", "b": "2", "probs": P(.05, .05, .85, .05, .95), "truth": []},  # confirmed, wrong
        {"a": "3", "b": "3", "probs": P(.05, .05, .75, .05, .95), "truth": ["mutually_exclusive"]},  # below bar: missed
        {"a": "4", "b": "4", "probs": P(.9, .9, .05, .05, .95), "truth": ["a_implies_b"]},  # "same outcome" claimed: too much
    ]
    s = lr.score(labels, 0.80, 0.90, 0.80)
    assert (s["right"], s["wrong"], s["missed"]) == (1, 2, 1)
    assert s["recall"] == 1 / 3
    loose = lr.score(labels, 0.70, 0.90, 0.80)
    assert loose["right"] == 2  # the 0.75 pair now counts
