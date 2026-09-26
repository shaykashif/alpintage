"""Replay the hand-checked labels (data/relation_labels.jsonl, with the market
rules from data/label_queue.jsonl) through structural.py and Jev: per family,
how many pairs the rules cover, how many match the label exactly, and any
relation the rules claim that the label says is false (EXTRA -- must stay 0).

    uv run python scripts/eval_structural.py
"""
import collections
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "src"))
from kalshi_engine import relations, structural  # noqa: E402

queue = {json.loads(l)["key"]: json.loads(l) for l in (PROJECT / "data" / "label_queue.jsonl").read_text(encoding="utf-8").splitlines()}
labels = [json.loads(l) for l in (PROJECT / "data" / "relation_labels.jsonl").read_text(encoding="utf-8").splitlines()]


def contract(ticker, m):
    return relations.Contract(venue="polymarket" if ticker.startswith("PM-") else "kalshi", ticker=ticker, event_id="",
                              category=None, title=m["title"], context=m["rules"] + "\n" + m["when"], close_time=None,
                              yes_bid=None, yes_ask=None)


stats = collections.defaultdict(collections.Counter)
extra_examples, missing_examples = [], []
final = collections.Counter()
for lab in labels:
    q = queue[lab["key"]]
    a, b = contract(q["a"], q["a_market"]), contract(q["b"], q["b_market"])
    truth = set(lab["truth"])
    v = structural.judge(a, b)
    jev = set(relations.classify(lab["probs"])[0])
    if v:
        got = set(v.relations)
        s = stats[v.family]
        s["covered"] += 1
        s["exact"] += got == truth
        if got - truth:
            s["EXTRA (dangerous)"] += 1
            extra_examples.append((v.family, q["a"], q["b"], sorted(got), sorted(truth), lab.get("note")))
        if truth - got:
            s["missing"] += 1
            missing_examples.append((v.family, q["a"], q["b"], sorted(got), sorted(truth), lab.get("note")))
        pred = got
    else:
        kept, _ = structural.restrict(a, b, sorted(jev))
        pred = set(kept)
        stats["(jev)"]["covered"] += 1
    if pred and pred <= truth:
        final["right"] += 1
    elif pred:
        final["WRONG"] += 1
        print("WRONG:", "rules" if v else "jev", q["a"], q["b"], sorted(pred), sorted(truth), lab.get("note"))
    elif truth:
        final["missed"] += 1

for fam, s in stats.items():
    print(fam, dict(s))
print("\nwith rules:", dict(final), f"recall {final['right'] / sum(1 for l in labels if l['truth']):.0%}")
print("\nEXTRA examples:")
for e in extra_examples[:25]:
    print("  ", e)
print("\nMISSING examples:")
for e in missing_examples[:25]:
    print("  ", e)
