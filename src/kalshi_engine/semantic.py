"""Meaning-based candidate pairs across venues.

relations.candidate_pairs only pairs markets whose titles share at least two
distinctive words, so the same event worded differently on Kalshi and
Polymarket ("CPI above 3.1%" vs "Will inflation exceed 3.1% in August?") is
never shown to Jev. This embeds every market title with a small local
sentence-embedding model (fastembed, BAAI/bge-small-en-v1.5 -- CPU, no API)
and pairs each Polymarket market with its nearest Kalshi markets by cosine
similarity. The pairs go to Jev like any other candidate, most similar
first; Jev's rulebook-level questions then decide.

Embeddings are cached on disk by title, so a scan only encodes titles it
hasn't seen (a first run encodes all ~11k in a few minutes on the VM).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .relations import Contract

MODEL_NAME = "BAAI/bge-small-en-v1.5"
CACHE_PATH = Path(__file__).resolve().parents[2] / "data" / "embed_cache.jsonl"
MODEL_DIR = Path(__file__).resolve().parents[2] / "data" / "models"  # not /tmp: survives reboots, downloaded once
DEFAULT_K = 5
# On 1,615 exported markets: >= 0.85 gave 474 cross-venue pairs, 249 of them
# never produced by the title-word matcher; below ~0.80 it's mostly noise.
DEFAULT_MIN_SIM = 0.85

_model = None


def _text(c: Contract) -> str:
    return c.title.replace(" -- ", ": ")


def _key(text: str) -> str:
    return hashlib.sha1(f"{MODEL_NAME}|{text}".encode()).hexdigest()[:20]


def _load_cache(path: Path) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                out[row["k"]] = row["v"]
    return out


def embed(texts: list[str], cache_path: Path = CACHE_PATH) -> np.ndarray:
    """Unit-length embeddings, one row per text."""
    global _model
    cache = _load_cache(cache_path)
    keys = [_key(t) for t in texts]
    missing = sorted({(k, t) for k, t in zip(keys, texts) if k not in cache})
    if missing:
        if _model is None:
            from fastembed import TextEmbedding  # imported lazily: heavy, and only the scanner needs it
            _model = TextEmbedding(MODEL_NAME, cache_dir=str(MODEL_DIR))
        vectors = list(_model.embed([t for _, t in missing]))
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("a", encoding="utf-8") as f:
            for (k, _), v in zip(missing, vectors):
                cache[k] = [round(float(x), 5) for x in v]
                f.write(json.dumps({"k": k, "v": cache[k]}) + "\n")
    m = np.array([cache[k] for k in keys], dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def cross_venue_pairs(contracts: list[Contract], k: int = DEFAULT_K, min_sim: float = DEFAULT_MIN_SIM,
                      cache_path: Path = CACHE_PATH) -> list[tuple[Contract, Contract, float]]:
    """Each Polymarket market's `k` nearest Kalshi markets with cosine
    similarity >= `min_sim`, as (a, b, similarity), lower ticker first,
    most similar first."""
    poly = [c for c in contracts if c.venue == "polymarket"]
    kalshi = [c for c in contracts if c.venue == "kalshi"]
    if not poly or not kalshi:
        return []
    vecs = embed([_text(c) for c in poly + kalshi], cache_path)
    sims = vecs[: len(poly)] @ vecs[len(poly):].T
    out: dict[tuple[str, str], tuple[Contract, Contract, float]] = {}
    top = np.argsort(-sims, axis=1)[:, :k]
    for i, row in enumerate(top):
        for j in row:
            s = float(sims[i, j])
            if s < min_sim:
                break
            a, b = sorted((poly[i], kalshi[j]), key=lambda c: c.ticker)
            out[(a.ticker, b.ticker)] = (a, b, s)
    return sorted(out.values(), key=lambda p: -p[2])
