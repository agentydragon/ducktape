"""The pinned weights, the pooling, and the query prefix have to actually retrieve."""

from __future__ import annotations

import math

import pytest
import pytest_bazel

from haku.state_index.embedder import BGE_SMALL_EN_V15, OnnxEmbedder, build_bge_small


@pytest.fixture(scope="module")
def embedder() -> OnnxEmbedder:
    return build_bge_small()


def _similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def test_reports_its_identity(embedder: OnnxEmbedder) -> None:
    assert (embedder.model_key, embedder.dims) == (BGE_SMALL_EN_V15, 384)


def test_embeds_to_unit_vectors_of_the_declared_width(embedder: OnnxEmbedder) -> None:
    vectors = embedder.embed_documents(["a short note", "another one entirely"])

    assert [len(vector) for vector in vectors] == [embedder.dims, embedder.dims]
    assert all(math.isclose(_similarity(vector, vector), 1.0, abs_tol=1e-5) for vector in vectors)


def test_a_query_is_closer_to_its_topic_than_to_an_unrelated_one(embedder: OnnxEmbedder) -> None:
    """Retrieval, not just arithmetic: the whole index rests on this ordering holding."""
    relevant, unrelated = embedder.embed_documents(
        [
            "Sourdough needs a starter fed twice a day before the dough will rise.",
            "The Kubernetes scheduler binds a pod to a node once its resource requests fit.",
        ]
    )
    query = embedder.embed_query("how do I get bread to rise")

    assert _similarity(query, relevant) > _similarity(query, unrelated)


def test_empty_input_costs_no_inference(embedder: OnnxEmbedder) -> None:
    assert embedder.embed_documents([]) == []


if __name__ == "__main__":
    pytest_bazel.main()
