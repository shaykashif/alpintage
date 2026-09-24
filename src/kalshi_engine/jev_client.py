"""Minimal client for TypeSafe's Jev (https://docs.typesafe.ai), used here only
for the Noul primitive: a single typed yes/no-with-probability judgment.

Endpoint and request/response shape are from TypeSafe's own docs
(docs.typesafe.ai/introduction, .../introduction/quickstart) as of 2026-09-21.
Re-check against current docs before relying on this beyond the POC — I have
not tested this against a real key.

No key configured -> falls back to a clearly-labelled mock so the collection
script is runnable without spending anything, but a mock proves nothing about
whether Jev is useful. Get a real TYPESAFE_API_KEY before trusting results.
"""
from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

import httpx

TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"


@dataclass
class NoulResult:
    prob: float  # P(yes), 0-1
    route: str  # "typesafe" or "mock"
    model: str
    latency_ms: float


def _ask_noul_real(api_key: str, state: str, instructions: str, timeout: float) -> NoulResult:
    t0 = time.monotonic()
    body = {
        "state": state,
        "model": "jev-latest",
        "questions": {
            "outcome": {"type": "noul", "instructions": instructions},
        },
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    r = httpx.post(TYPESAFE_URL, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    answer = data["answers"]["outcome"]
    return NoulResult(
        prob=float(answer["noul"]),
        route="typesafe",
        model=data.get("model", "jev-latest"),
        latency_ms=(time.monotonic() - t0) * 1000,
    )


def _ask_noul_mock(state: str, instructions: str) -> NoulResult:
    """Deterministic, clearly-fake stand-in: a hash of the state text mapped to
    [0, 1]. Same input always gives the same output, so a re-run doesn't churn,
    but it carries zero information about the real question. Never mistake a
    mock result for evidence."""
    t0 = time.monotonic()
    h = hashlib.sha256((instructions + "||" + state).encode()).hexdigest()
    prob = int(h[:8], 16) / 0xFFFFFFFF
    return NoulResult(
        prob=round(prob, 4),
        route="mock",
        model="mock-jev",
        latency_ms=(time.monotonic() - t0) * 1000,
    )


@dataclass
class NoulMultiResult:
    probs: dict[str, float]  # question name -> P(yes)
    route: str  # "typesafe" or "mock"
    model: str


def ask_noul_multi(state: str, questions: dict[str, str], timeout: float = 30.0) -> NoulMultiResult:
    """Several Noul yes/no judgments about the same `state` in ONE call
    (the API takes a dict of named questions -- verified live 2026-09-24).
    With no key, every answer is 0.0 and route="mock": a mock must never
    look like a confident judgment to a caller that trades on it."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if not api_key:
        return NoulMultiResult(probs={name: 0.0 for name in questions}, route="mock", model="mock-jev")
    body = {
        "state": state,
        "model": "jev-latest",
        "questions": {name: {"type": "noul", "instructions": text} for name, text in questions.items()},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    # Transient 503s/429s were seen live under 8-way parallel load: retry a
    # couple of times with backoff before letting the caller defer the item.
    for attempt in range(3):
        r = httpx.post(TYPESAFE_URL, json=body, headers=headers, timeout=timeout)
        if r.status_code not in (429, 500, 502, 503, 504, 529) or attempt == 2:
            break
        time.sleep(0.8 * 2 ** attempt)
    r.raise_for_status()
    data = r.json()
    return NoulMultiResult(
        probs={name: float(data["answers"][name]["noul"]) for name in questions},
        route="typesafe",
        model=data.get("model", "jev-latest"),
    )


def ask_noul(state: str, instructions: str, timeout: float = 15.0) -> NoulResult:
    """Ask Jev's Noul primitive a yes/no judgment about `state`. Uses
    TYPESAFE_API_KEY from the environment if set, otherwise the mock."""
    api_key = os.environ.get("TYPESAFE_API_KEY")
    if api_key:
        return _ask_noul_real(api_key, state, instructions, timeout)
    return _ask_noul_mock(state, instructions)
