"""The client and the service, tested against each other.

`OpenAIEmbedder` talks a standard wire format precisely so it can be pointed at Ollama or LiteLLM
later, which means nothing in this repo would notice if our service stopped speaking it correctly.
So the client here is the real `openai` one, driven over ASGI against the real app: no fake
transport, no hand-written JSON, and the round trip proves the same property `test_embedder.py`
asserts locally — that a query ranks its own topic first.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_bazel
from openai import AsyncOpenAI

from haku.embedder_service.main import MAX_INPUTS, build_app
from haku.state_index.embedder import BGE_SMALL_EN_V15, build_bge_small
from haku.state_index.openai_embedder import OpenAIEmbedder


@pytest.fixture(scope="session")
def app():
    return build_app(build_bge_small(threads=1))


@pytest.fixture
async def client(app) -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url="http://embedder/v1",
        api_key="not-used",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://embedder"),
    )


async def test_a_query_ranks_its_topic_over_an_unrelated_document(client: AsyncOpenAI) -> None:
    embedder = OpenAIEmbedder(client, model=BGE_SMALL_EN_V15)
    about, unrelated = await embedder.embed_documents(
        ["Postgres stores the approval ledger.", "The bicycle needs a new chain."]
    )
    query = await embedder.embed_query("where are tool call approvals kept")

    def similarity(vector: list[float]) -> float:
        return sum(left * right for left, right in zip(query, vector, strict=True))

    assert similarity(about) > similarity(unrelated)


async def test_vectors_come_back_paired_with_the_input_that_produced_them(client: AsyncOpenAI) -> None:
    embedder = OpenAIEmbedder(client, model=BGE_SMALL_EN_V15)
    first, second = await embedder.embed_documents(["alpha", "beta"])
    (alpha_again,) = await embedder.embed_documents(["alpha"])
    assert first == alpha_again
    assert second != alpha_again


async def test_the_query_instruction_is_the_clients_to_apply(client: AsyncOpenAI) -> None:
    """The endpoint is model-generic, so bge's asymmetry has to live client-side."""
    embedder = OpenAIEmbedder(client, model=BGE_SMALL_EN_V15)
    assert await embedder.embed_query("intake") != (await embedder.embed_documents(["intake"]))[0]
    plain = OpenAIEmbedder(client, model=BGE_SMALL_EN_V15, query_instruction="")
    assert await plain.embed_query("intake") == (await embedder.embed_documents(["intake"]))[0]


async def test_asking_for_a_model_the_server_does_not_serve_is_an_error(client: AsyncOpenAI) -> None:
    """Rather than embedding into a different vector space than the corpus holds."""
    with pytest.raises(Exception, match="not served here"):
        await OpenAIEmbedder(client, model="some-other-model").embed_documents(["intake"])


async def test_an_oversized_batch_is_refused_rather_than_attempted(client: AsyncOpenAI) -> None:
    with pytest.raises(Exception, match="at most"):
        await OpenAIEmbedder(client, model=BGE_SMALL_EN_V15).embed_documents(["x"] * (MAX_INPUTS + 1))


async def test_embedding_nothing_asks_the_server_nothing(client: AsyncOpenAI) -> None:
    assert await OpenAIEmbedder(client, model=BGE_SMALL_EN_V15).embed_documents([]) == []


if __name__ == "__main__":
    pytest_bazel.main()
