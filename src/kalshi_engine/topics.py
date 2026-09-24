"""Decides whether an event is CULTURAL, ECONOMIC, GEOPOLITICAL, or out of
scope for the relationship scanner (the project owner's scope; geopolitics
-- wars, conflicts, diplomacy, sanctions -- added 2026-09-24).

Two tiers, so Jev's calls go where judgment is actually needed:
  1. Rules for the clear-cut cases -- a Kalshi category like "Economics"
     or "Sports", or a Polymarket fee schedule named after its topic
     ("culture_fees", "sports_fees_v3"). No call needed.
  2. Jev for the grey areas -- Kalshi "Politics", "Science and Technology",
     "Crypto", "World", uncategorized events; Polymarket markets whose fee
     schedule says nothing useful (e.g. "zero_fees", "politics_fees").
     One Noul call per event asks three questions (primarily cultural?
     economic? geopolitical?), reading the event title, category hint and
     a few of its markets. Verdicts are cached in data/topic_cache.jsonl,
     keyed on the event AND a hash of the text Jev saw, so an event is
     only re-asked if its wording changes.

A mock answer (no TYPESAFE_API_KEY) is never cached and never admits an
event: an unjudged grey event is left out, not guessed in.
"""
from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .jev_client import ask_noul_multi

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "topic_cache.jsonl"

CULTURAL, ECONOMIC, GEOPOLITICAL = "cultural", "economic", "geopolitical"
TOPICS = (CULTURAL, ECONOMIC, GEOPOLITICAL)

# Bump when TOPIC_QUESTIONS change: it's part of every cached verdict's key,
# so a new definition re-judges events instead of reusing old answers.
QUESTIONS_VERSION = 2

# Kalshi event categories (GET /events, verified 2026-09-24).
KALSHI_RULES = {
    "Economics": ECONOMIC, "Financials": ECONOMIC, "Commodities": ECONOMIC, "Companies": ECONOMIC,
    "Entertainment": CULTURAL, "Social": CULTURAL, "Mentions": CULTURAL,
    "Sports": None, "Elections": None, "Climate and Weather": None,
    # "World" and "Politics" stay grey: Jev separates geopolitics (wars,
    # diplomacy) from domestic politics, which stays out of scope.
}
# Polymarket fee-schedule names that already say the topic. Anything else
# (crypto, tech, politics, zero_fees, missing) is a grey area for Jev.
POLY_RULES = {
    "culture_fees": CULTURAL, "mentions_fees": CULTURAL,
    "economics_fees": ECONOMIC, "finance_prices_fees": ECONOMIC,
    "weather_fees": None,
}

# Jev must be at least this sure an event is in one of the topics to admit it.
TOPIC_THRESHOLD = 0.60

TOPIC_QUESTIONS = {
    CULTURAL: (
        "Is this prediction-market event primarily CULTURAL -- entertainment, film, TV, "
        "streaming, music, charts, awards, celebrities, social media and internet culture, "
        "what public figures will say (word 'mentions'), media, or society and lifestyle "
        "trends? Answer NO for sports competitions, and NO if it is mainly about "
        "politics, government, elections, weather, or science."
    ),
    ECONOMIC: (
        "Is this prediction-market event primarily ECONOMIC or FINANCIAL -- macroeconomic "
        "data (inflation, jobs, GDP, interest rates), central banks, prices of assets, "
        "commodities or currencies (including crypto prices), companies and their "
        "products or earnings, trade, or financial markets? Answer NO if it is mainly "
        "about politics, elections, sports, weather, or entertainment."
    ),
    GEOPOLITICAL: (
        "Is this prediction-market event primarily GEOPOLITICAL -- wars and armed "
        "conflicts, military operations and territorial control, ceasefires and peace "
        "deals, diplomacy and summits between countries, sanctions, international "
        "treaties or organizations, or relations between states? Answer NO if it is "
        "mainly about DOMESTIC politics (a country's own elections, legislation, "
        "approval ratings, or officials' internal actions), or about sports, "
        "entertainment, weather, or markets."
    ),
}


@dataclass
class Subject:
    """One event to place in a topic. `hint` is the venue's own label."""
    key: str  # "kalshi:<event_ticker>" or "polymarket:<event slug>"
    venue: str
    title: str
    hint: str | None
    sample: list[str]  # a few market titles/outcomes in the event

    @property
    def text(self) -> str:
        lines = [f"Venue: {self.venue}", f"Event: {self.title}", f"Venue's category label: {self.hint or 'none'}"]
        if self.sample:
            lines.append("Example markets: " + "; ".join(self.sample[:4]))
        return "\n".join(lines)

    @property
    def text_hash(self) -> str:
        return hashlib.sha1(f"v{QUESTIONS_VERSION}|{self.text}".encode("utf-8")).hexdigest()[:12]


def rule_topic(s: Subject) -> tuple[bool, str | None]:
    """(decided, topic). decided=False means a grey area for Jev."""
    rules = KALSHI_RULES if s.venue == "kalshi" else POLY_RULES
    if s.venue == "polymarket" and (s.hint or "").startswith("sports"):
        return True, None
    if s.hint in rules:
        return True, rules[s.hint]
    return False, None


def decide(probs: dict[str, float], threshold: float = TOPIC_THRESHOLD) -> str | None:
    best = max(TOPICS, key=lambda t: probs.get(t, 0.0))
    return best if probs.get(best, 0.0) >= threshold else None


class TopicClassifier:
    def __init__(self, cache_path: Path | str = CACHE_PATH, max_new: int = 1500, workers: int = 8, ask=ask_noul_multi):
        self.cache_path = Path(cache_path)
        self.max_new, self.workers, self.ask = max_new, workers, ask
        self.cache: dict[str, dict] = {}
        if self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.cache[row["key"]] = row
        self.stats = {"rule": 0, "cached": 0, "asked": 0, "deferred": 0}

    def _ask(self, s: Subject) -> dict | None:
        result = self.ask(s.text, TOPIC_QUESTIONS)
        if result.route != "typesafe":
            return None  # never cache or trust a mock
        return {
            "key": s.key, "text_hash": s.text_hash, "title": s.title, "hint": s.hint,
            "probs": result.probs, "topic": decide(result.probs),
            "asked_at": datetime.now(timezone.utc).isoformat(),
        }

    def classify(self, subjects: list[Subject]) -> dict[str, str | None]:
        """topic (or None = out of scope / not yet judged) for every subject.
        Grey subjects beyond this run's `max_new` budget are left out for
        now and judged on a later run."""
        out: dict[str, str | None] = {}
        todo: list[Subject] = []
        for s in subjects:
            decided, topic = rule_topic(s)
            if decided:
                out[s.key] = topic
                self.stats["rule"] += 1
                continue
            hit = self.cache.get(s.key)
            if hit and hit.get("text_hash") == s.text_hash:
                out[s.key] = hit.get("topic")
                self.stats["cached"] += 1
                continue
            todo.append(s)
        self.stats["deferred"] = max(0, len(todo) - self.max_new)
        for s in todo[self.max_new:]:
            out[s.key] = None
        new_rows = []
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self._ask, s): s for s in todo[: self.max_new]}
            for fut in as_completed(futures):
                s = futures[fut]
                try:
                    row = fut.result()
                except Exception as exc:  # noqa: BLE001 -- a failed call just defers this event
                    print(f"  topic: Jev failed on {s.key}: {exc}")
                    row = None
                out[s.key] = row["topic"] if row else None
                if row:
                    self.cache[s.key] = row
                    new_rows.append(row)
        self.stats["asked"] = len(new_rows)
        if new_rows:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cache_path.open("a", encoding="utf-8") as f:
                for row in new_rows:
                    f.write(json.dumps(row) + "\n")
        return out
