"""One-off: resize the open relation-arb trades as if the $100-per-leg sizing
(risk.py / relations.py, raised 2026-09-24) had applied when they were
entered -- multiply each open set's quantity by FACTOR -- and rescale the
equity curve's relation-arb history to match.

Only whole sets whose every leg is still open (per the latest
paper_pnl_summary.json) are resized; a set with a settled or sold leg is
left alone. The equity history is only rescaled when every relation-arb
set ever traded is being resized -- then the strategy's PnL at each point
is exactly FACTOR times what it was (fees are ~linear in quantity);
otherwise the history is left as-is and the script says so.

Each resized fill row gets `rescaled: FACTOR` and its original quantity,
so the change is visible in the ledger. Backups of both files are written
next to them first. Idempotent: rows already rescaled are skipped.

Stop the watcher and loop first so nothing appends while this rewrites,
then re-score:

    sudo systemctl stop kalshi-watcher.service kalshi-loop.service
    sudo .venv/bin/python scripts/rescale_open_trades.py            # dry run
    sudo .venv/bin/python scripts/rescale_open_trades.py --apply
    sudo .venv/bin/python scripts/score_paper_fills.py
    sudo systemctl start kalshi-watcher.service kalshi-loop.service
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kalshi_engine import ledger  # noqa: E402
from kalshi_engine.fees import taker_fee  # noqa: E402

DATA = Path(__file__).resolve().parent.parent / "data"
FILLS_PATH = ledger.DEFAULT_LOG_PATH
SUMMARY_PATH = DATA / "paper_pnl_summary.json"
EQUITY_PATH = DATA / "paper_equity.jsonl"
STRATEGY = "relation_arb"
FACTOR = 10


def scaled_fill(row: dict, factor: int) -> dict:
    qty = row["qty"] * factor
    if row.get("venue") == "kalshi":
        fee = float(taker_fee(qty, row["price"]))  # Kalshi rounds up per order: recompute
    else:
        fee = round(row.get("fee_usd", 0.0) * factor, 6)  # Polymarket's fee is linear in qty
    return {**row, "qty": qty, "fee_usd": fee, "cost_usd": round(row["price"] * qty + fee, 6),
            "rescaled": factor, "qty_original": row["qty"]}


def scaled_equity(row: dict, factor: int) -> dict:
    row = json.loads(json.dumps(row))
    extra = factor - 1
    det = (row.get("by_strategy_detail") or {}).get(STRATEGY)
    if det:
        deltas = {k: det.get(k, 0.0) * extra for k in ("total_pnl", "realized_pnl", "unrealized_pnl", "open_cost")}
        for k, d in deltas.items():
            det[k] = round(det.get(k, 0.0) + d, 4)
            if k in row:
                row[k] = round(row[k] + d, 4)
    else:
        total = (row.get("by_strategy") or {}).get(STRATEGY)
        if total is None:
            return row
        deltas = {"total_pnl": total * extra, "unrealized_pnl": total * extra}
        for k, d in deltas.items():
            if k in row:
                row[k] = round(row[k] + d, 4)
    if STRATEGY in (row.get("by_strategy") or {}):
        row["by_strategy"][STRATEGY] = round(row["by_strategy"][STRATEGY] * factor, 4)
    if "equity" in row:
        row["equity"] = round(row["equity"] + deltas["total_pnl"], 2)
    return row


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    ap.add_argument("--factor", type=int, default=FACTOR)
    args = ap.parse_args()

    rows = ledger.load_rows(FILLS_PATH)
    summary = json.loads(SUMMARY_PATH.read_text(encoding="utf-8"))
    status = {p["ticker"]: p["status"] for p in summary.get("positions", [])}

    groups: dict[str, set[str]] = {}
    for r in rows:
        if r.get("event") in ("fill", "sell") and ledger.strategy_of(r) == STRATEGY and r.get("arb_group"):
            groups.setdefault(r["arb_group"], set()).add(r["ticker"])
    sold = {r["ticker"] for r in rows if r.get("event") == "sell"}
    done = {r["arb_group"] for r in rows if r.get("rescaled")}

    resize = {g for g, tickers in groups.items()
              if g not in done and not tickers & sold and all(status.get(t) == "open" for t in tickers)}
    skipped = set(groups) - resize - done
    tickers = set().union(*(groups[g] for g in resize)) if resize else set()

    print(f"{len(groups)} relation-arb set(s): {len(resize)} to resize x{args.factor}, "
          f"{len(done)} already resized, {len(skipped)} left alone (a leg settled or sold)")
    new_rows, cost_before, cost_after = [], 0.0, 0.0
    for r in rows:
        if r.get("event") == "fill" and r.get("arb_group") in resize:
            s = scaled_fill(r, args.factor)
            cost_before += r["cost_usd"]
            cost_after += s["cost_usd"]
            print(f"  {r['ticker']} {r['side'].upper()} {r['qty']:g} -> {s['qty']:g} @{r['price']}  "
                  f"cost ${r['cost_usd']:.2f} -> ${s['cost_usd']:.2f}")
            r = s
        new_rows.append(r)
    print(f"open cost ${cost_before:.2f} -> ${cost_after:.2f}")

    whole_history = not skipped and not done and bool(resize)
    if not whole_history:
        print("equity history NOT rescaled: some relation-arb sets aren't being resized, "
              "so the strategy's past PnL can't be split per set")

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply.")
        return
    if not resize:
        return

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with ledger.ledger_lock():
        shutil.copy2(FILLS_PATH, FILLS_PATH.with_name(f"{FILLS_PATH.name}.bak-{stamp}"))
        tmp = FILLS_PATH.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in new_rows), encoding="utf-8")
        tmp.replace(FILLS_PATH)
    print(f"rewrote {FILLS_PATH} (backup .bak-{stamp})")

    if whole_history and EQUITY_PATH.exists():
        shutil.copy2(EQUITY_PATH, EQUITY_PATH.with_name(f"{EQUITY_PATH.name}.bak-{stamp}"))
        eq = [json.loads(line) for line in EQUITY_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
        tmp = EQUITY_PATH.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(scaled_equity(r, args.factor)) + "\n" for r in eq), encoding="utf-8")
        tmp.replace(EQUITY_PATH)
        print(f"rescaled {len(eq)} equity point(s) in {EQUITY_PATH} (backup .bak-{stamp})")
    print("now run scripts/score_paper_fills.py, then start the services again")


if __name__ == "__main__":
    main()
