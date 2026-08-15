"""An `Embedder` over any OpenAI-compatible `/v1/embeddings` endpoint.

The wire format is the one everything speaks — our own embedding service
(`haku/embedder_service`), Ollama, LiteLLM, OpenAI itself — so moving between them is a base URL
and a model name, not an implementation. That is the whole reason for choosing it over something
bespoke: the destination for this is expected to change.

Two things the endpoint does not do for you, both because it is model-generic:

- **The query instruction is applied here.** bge is trained asymmetrically, and nothing on the
  wire says so; a caller pointing this at a model with no such asymmetry passes
  `query_instruction=""`.
- **`model_key` is the configured model, and a mismatch is fatal.** Vectors from two models share
  a space only by coincidence — cosine between them is well-defined and meaningless — so a server
  that answers as something else must not be allowed to quietly write into a corpus embedded by
  the first.
"""

from __future__ import annotations

from collections.abc import Sequence

from openai import AsyncOpenAI

from haku.state_index.embedder import BGE_QUERY_INSTRUCTION


class OpenAIEmbedder:
    """Embeddings from an OpenAI-compatible endpoint."""

    def __init__(self, client: AsyncOpenAI, *, model: str, query_instruction: str = BGE_QUERY_INSTRUCTION) -> None:
        self._client = client
        self._model = model
        self._query_instruction = query_instruction

    @property
    def model_key(self) -> str:
        """The model, not the transport.

        So pointing the same model at a different server — our service today, Ollama later —
        re-uses every cached vector instead of re-embedding the corpus for no reason.
        """
        return self._model

    async def _embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []
        response = await self._client.embeddings.create(model=self._model, input=inputs)
        if response.model != self._model:
            raise ValueError(
                f"embedding server answered as {response.model!r} for a request for {self._model!r}; "
                "configure the name the server reports, or the corpus ends up holding two vector spaces"
            )
        # Order is by `index`, not by arrival: the API does not promise the response array matches
        # the request's, and a silently reordered batch would pair every chunk with another's vector.
        return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(list(texts))

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self._embed([self._query_instruction + text])
        return vector
