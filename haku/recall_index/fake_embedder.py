"""A deterministic embedder for database tests — no embedding service, no network.

Shared by both corpora's sync tests: what they assert is the index's bookkeeping (what got
embedded, what got re-used, what stayed reachable), and a real embedder would make those tests
slow and flaky without making them stronger. Nothing in this package exercises the real one; the
CLI is how `OpenAIEmbedder` gets pointed at a live endpoint.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

# A vector space with one axis per marker word, so "which document is about beta" has an
# answer a test can assert. The floor keeps every vector non-zero: cosine distance against a
# zero vector is undefined, and a chunk mentioning none of the markers is normal.
MARKERS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta")


class FakeEmbedder:
    """Deterministic marker-word embedder."""

    dims = len(MARKERS)

    def __init__(self, model_key: str = "fake-v1") -> None:
        self.model_key = model_key

    def _vector(self, text: str) -> list[float]:
        counts = [text.lower().count(marker) + 0.01 for marker in MARKERS]
        norm = math.sqrt(sum(count * count for count in counts))
        return [count / norm for count in counts]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


class ExplodingEmbedder(FakeEmbedder):
    """Fails once it has embedded anything at all, to cut a sync off mid-flight."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("embedder unavailable")
