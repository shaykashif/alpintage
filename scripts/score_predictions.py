"""Score logged predictions against real settlement outcomes.

For every row in data/predictions.jsonl whose market has since settled, fetch
the result, fill in `outcome`, and report Brier scores for Jev vs the market's
own price at logging time. Lower Brier score = better calibrated. This is the
whole point of the POC: does Jev's judgment beat what the market already knew?

Usage:
    uv run python scripts/score_predictions.py
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine.kalshi_public import PublicClient  # noqa: E402

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "predictions.jsonl"


def main() -> None:
    if not DATA_PATH.exists():
        print(f"no data yet at {DATA_PATH}; run collect_predictions.py first")
        return

    rows = [json.loads(line) for line in DATA_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    client = PublicClient()

    newly_scored = 0
    for row in rows:
        if row.get("outcome") is not None:
            continue
        try:
            m = client.market(row["ticker"])
        except Exception as exc:  # noqa: BLE001
            print(f"  {row['ticker']}: could not fetch ({exc}), skipping")
            continue

        time.sleep(0.15)  # be polite to Kalshi's API even for the (usually more common) unsettled case
        status = m.get("status")
        if status != "settled":
            continue

        result = m.get("result")  # expected "yes" or "no" -- verify against docs
        if result not in ("yes", "no"):
            print(f"  {row['ticker']}: settled but result field is '{result}', skipping")
            continue

        row["outcome"] = 1 if result == "yes" else 0
        row["scored_at"] = datetime.now(timezone.utc).isoformat()
        newly_scored += 1
        print(f"  {row['ticker']}: settled -> {result}")
        time.sleep(0.1)

    if newly_scored:
        with DATA_PATH.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row) + "\n")
        print(f"scored {newly_scored} newly-settled markets, rewrote {DATA_PATH}")

    scored = [r for r in rows if r.get("outcome") is not None]
    print(f"\n{len(scored)} of {len(rows)} logged predictions are settled and scored")

    if len(scored) < 5:
        print("not enough settled markets yet for a meaningful comparison (want 20+, ideally 100+)")
        return

    jev_brier = _brier(scored, "jev_prob")
    market_brier = _brier(scored, "market_prob")

    print(f"\nBrier score (lower is better; 0.25 = always guessing 0.5):")
    print(f"  Jev:    {jev_brier:.4f}")
    print(f"  Market: {market_brier:.4f}")

    if jev_brier < market_brier:
        print("\n-> Jev's judgment was better calibrated than the market's price on this sample.")
    else:
        print("\n-> The market's price beat Jev's judgment on this sample.")
    print("Treat this as a first read, not a conclusion, until the sample is larger and stable.")

    mock_rows = [r for r in scored if r.get("jev_route") == "mock"]
    if mock_rows:
        print(
            f"\nNOTE: {len(mock_rows)} of {len(scored)} scored rows used the mock Jev client. "
            "Those carry no information -- set TYPESAFE_API_KEY and re-collect for a real test."
        )


def _brier(rows: list[dict], prob_key: str) -> float:
    return sum((r[prob_key] - r["outcome"]) ** 2 for r in rows) / len(rows)


if __name__ == "__main__":
    main()
