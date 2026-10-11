"""What the index needs from an embedding backend.

One method pair and an identity. `model_key` is part of the chunk cache key, so it names the
model rather than where it runs: the same model behind a different endpoint re-uses every cached
vector, and a different model misses by construction instead of quietly mixing two vector spaces
into one corpus.

Async because embedding is a network call — `openai_embedder.OpenAIEmbedder` against Ollama today,
against anything else that speaks `/v1/embeddings` tomorrow.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


class Embedder(Protocol):
    @property
    def model_key(self) -> str:
        """Stable identifier of the model, part of the chunk cache key."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
