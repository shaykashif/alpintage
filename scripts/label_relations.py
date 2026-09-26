"""Hand-label the relations Jev judged, then recalibrate its thresholds.

The confirmation bars in relations.py (RELATION_THRESHOLD, GATE_THRESHOLD,
IMPLICATION_GATE_THRESHOLD) were set from 17 labeled pairs, and at those
bars about half the true relations were rejected. This shows you judged
pairs one at a time -- both markets' titles and rules, and what Jev said --
and records what the relation really is. `--report` then replays every
labeled pair through relations.classify at a grid of bars and shows which
settings confirm the most true relations without confirming a false one.

Pairs are shown nearest the current relation bar first (the ones whose
label says the most about where the bar should sit). Labels are appended to
data/relation_labels.jsonl; re-labeling a pair keeps the latest answer.

Run on the server, where the full judgment cache is:
    uv run python scripts/label_relations.py            # label (q to stop any time)
    uv run python scripts/label_relations.py --report   # what the labels say about the bars
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import relations  # noqa: E402
from kalshi_engine.kalshi_public import PublicClient  # noqa: E402
from kalshi_engine.relation_sources import fetch_polymarket_market  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA / "relations_cache.jsonl"
LABELS_PATH = DATA / "relation_labels.jsonl"

NAMES = {"a_implies_b": "if A then B", "b_implies_a": "if B then A",
         "mutually_exclusive": "not both", "exhaustive": "at least one"}
KEYS = {"1": ["a_implies_b"], "2": ["b_implies_a"], "3": ["mutually_exclusive"], "4": ["exhaustive"],
        "5": ["a_implies_b", "b_implies_a"]}
MIN_BEST = 0.5  # pairs Jev gave no relation even this much are clearly unrelated: not worth your time


def load_latest(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out[row["key"]] = row
    return out


def best(probs: dict) -> tuple[str, float]:
    r = max(relations.RELATIONS, key=lambda k: probs.get(k, 0.0))
    return r, probs.get(r, 0.0)


# ---- showing a market --------------------------------------------------------------

_client: PublicClient | None = None
_markets: dict[str, dict] = {}


def market_text(ticker: str) -> dict:
    """Title, rules and timing for one market, fetched live (the cache keeps
    only tickers and probabilities)."""
    global _client
    if ticker in _markets:
        return _markets[ticker]
    info = {"title": ticker, "rules": "(couldn't fetch rules)", "when": "?", "venue": "?"}
    try:
        if ticker.startswith("PM-"):
            pm = fetch_polymarket_market(ticker[3:])
            if pm:
                info = {"venue": "Polymarket", "title": pm.get("question") or ticker,
                        "rules": pm.get("description") or "", "when": f"ends {pm.get('endDate')}"}
        else:
            _client = _client or PublicClient()
            m = _client.market(ticker)
            rules = m.get("rules_primary") or ""
            if m.get("rules_secondary"):
                rules += "\n" + m["rules_secondary"]
            info = {"venue": "Kalshi", "title": f"{m.get('title', '')} -- {m.get('yes_sub_title') or ''}".strip(" -"),
                    "rules": rules, "when": f"closes {m.get('close_time')}, settles {m.get('expected_expiration_time')}"}
    except Exception as exc:  # noqa: BLE001 -- show what we can
        info["rules"] = f"(couldn't fetch: {exc})"
    _markets[ticker] = info
    return info


def show(label: str, ticker: str, full: bool) -> None:
    m = market_text(ticker)
    print(f"\n  {label}  [{m['venue']}] {m['title']}")
    print(f"     {ticker} -- {m['when']}")
    rules = m["rules"] if full else textwrap.shorten(m["rules"].replace("\n", " "), 420, placeholder=" ...")
    for line in textwrap.wrap(rules, 96):
        print(f"     {line}")


# ---- labeling -----------------------------------------------------------------------

def label(limit: int) -> None:
    cache = load_latest(CACHE_PATH)
    done = load_latest(LABELS_PATH)
    todo = [r for k, r in cache.items() if k not in done and r.get("route") == "typesafe"
            and best(r["probs"])[1] >= MIN_BEST]
    todo.sort(key=lambda r: abs(best(r["probs"])[1] - relations.RELATION_THRESHOLD))
    print(f"{len(done)} labeled so far; {len(todo)} judged pairs worth labeling. q to stop.")
    n = 0
    for row in todo[:limit]:
        probs = row["probs"]
        verdict, why = relations.classify(probs)
        full = False
        while True:
            print("\n" + "-" * 100)
            show("A", row["a"], full)
            show("B", row["b"], full)
            print("\n  Jev: " + "  ".join(f"{NAMES[r]} {probs.get(r, 0):.2f}" for r in relations.RELATIONS)
                  + f"  | same underlying {probs.get('same_underlying', 0):.2f}")
            print("  Pternas currently: " + (", ".join(NAMES[r] for r in verdict) if verdict else f"no relation ({why or 'below bar'})"))
            ans = input("\n  What's TRUE? 1 if A then B  2 if B then A  3 not both  4 at least one  5 same outcome"
                        "  0 unrelated  (combine, e.g. 34)  r full rules  s skip  q quit\n  > ").strip().lower()
            if ans == "q":
                print(f"\nsaved {n} label(s) this session")
                return
            if ans == "s":
                break
            if ans == "r":
                full = True
                continue
            if ans == "0":
                truth: list[str] = []
            elif ans and all(ch in KEYS for ch in ans):
                truth = sorted({r for ch in ans for r in KEYS[ch]})
            else:
                print("  ? type digits from the list, or r / s / q")
                continue
            with LABELS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"key": row["key"], "a": row["a"], "b": row["b"], "truth": truth,
                                    "probs": probs, "model": row.get("model"),
                                    "labeled_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            n += 1
            break
    print(f"\nsaved {n} label(s) this session")


# ---- calibration report -----------------------------------------------------------

def score(labels: list[dict], t: float, g: float, ig: float) -> dict:
    right = wrong = missed = 0
    wrong_keys = []
    for lab in labels:
        pred = set(relations.classify(lab["probs"], t, g, ig)[0])
        truth = set(lab["truth"])
        if pred and pred <= truth:
            right += 1
        elif pred:
            wrong += 1
            wrong_keys.append(lab)
        elif truth:
            missed += 1
    true_total = sum(1 for lab in labels if lab["truth"])
    return {"t": t, "g": g, "ig": ig, "right": right, "wrong": wrong, "missed": missed,
            "recall": right / true_total if true_total else 0.0, "wrong_labels": wrong_keys}


def report() -> None:
    labels = list(load_latest(LABELS_PATH).values())
    true_total = sum(1 for lab in labels if lab["truth"])
    print(f"{len(labels)} labeled pair(s): {true_total} with a real relation, {len(labels) - true_total} unrelated")
    if len(labels) < 30:
        print("(fewer than 30 labels -- treat what follows as a rough read)")
    now = score(labels, relations.RELATION_THRESHOLD, relations.GATE_THRESHOLD, relations.IMPLICATION_GATE_THRESHOLD)
    print(f"\ncurrent bars (relation {now['t']:.2f}, gate {now['g']:.2f}, implication gate {now['ig']:.2f}): "
          f"{now['right']} confirmed correctly, {now['wrong']} WRONG, {now['missed']} missed "
          f"(recall {now['recall']:.0%})")
    for lab in now["wrong_labels"]:
        print(f"  WRONG today: {lab['a']} / {lab['b']} -- truth {lab['truth'] or 'unrelated'}")

    grid = [round(x * 0.05, 2) for x in range(10, 20)]  # 0.50 .. 0.95
    results = [score(labels, t, g, ig) for t, g, ig in itertools.product(grid, grid, grid)]
    safe = [r for r in results if r["wrong"] == 0 and r["right"] > 0]
    # Most true relations first; among ties, the strictest bars (most margin).
    safe.sort(key=lambda r: (-r["right"], -(r["t"] + r["g"] + r["ig"])))
    print("\nbars that confirm NO false relation among your labels, most found first:")
    for r in safe[:10]:
        print(f"  relation {r['t']:.2f}  gate {r['g']:.2f}  implication gate {r['ig']:.2f}  ->  "
              f"{r['right']} of {true_total} found (recall {r['recall']:.0%})")
    if not safe:
        print("  none yet -- label more pairs")
    print("\nPick one with some margin rather than the loosest, and send it to me (or pass it to"
          " run_relation_scanner.py as --threshold / --gate-threshold / --implication-gate-threshold).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true", help="calibration report from the labels so far")
    ap.add_argument("--limit", type=int, default=500, help="max pairs to show this session")
    args = ap.parse_args()
    report() if args.report else label(args.limit)


if __name__ == "__main__":
    main()
