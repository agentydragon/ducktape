"""Embedding backends for the haku index.

Everything downstream depends only on the `Embedder` protocol, and `model_key` is part of the
chunk cache key — so swapping the model invalidates cached vectors by construction rather than
by anyone remembering to. The protocol is async because the deployed backend is a network call
(`openai_embedder.OpenAIEmbedder`); this one is CPU work, and offloads to a thread so a caller's
event loop is not blocked either way.

This backend is what the local evaluation CLI and the embedding service itself run: weights from
Bazel-pinned `http_file`s rather than a runtime download, because the service runs in-cluster
behind an egress fence and a model that fetches itself on first use is a startup failure waiting
for the day the fence tightens.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime
from tokenizers import Tokenizer

from util.bazel import runfiles

# BAAI/bge-small-en-v1.5: 384-dim, 33M params, CLS pooling, ~2 KiB/s/core on prose.
BGE_SMALL_EN_V15 = "bge-small-en-v1.5"
_BGE_DIMS = 384
_MAX_TOKENS = 512

# bge is trained asymmetrically: queries get this instruction prefix, documents get none.
# Embedding a query without it measurably degrades retrieval. Public because a client talking to
# a remote embedder has to apply it itself — the wire is model-generic, so the asymmetry is the
# caller's knowledge, not the endpoint's.
BGE_QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    @property
    def model_key(self) -> str:
        """Stable identifier of the model + pooling, part of the chunk cache key."""

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class OnnxEmbedder:
    """bge-small-en-v1.5 under onnxruntime, CPU-only.

    Weights come from Bazel-pinned `http_file`s (see MODULE.bazel), not a runtime download:
    the sync job runs in-cluster behind a force-proxy egress fence, and a model that fetches
    itself on first use is a startup failure waiting for the one day the fence tightens.
    """

    def __init__(self, model_path: Path, tokenizer_path: Path, threads: int = 0) -> None:
        options = onnxruntime.SessionOptions()
        # 0 lets onnxruntime pick; pinned deployments set it to the pod's CPU limit so the
        # runtime doesn't spawn a thread per host core and thrash against the cgroup quota.
        options.intra_op_num_threads = threads
        self._session = onnxruntime.InferenceSession(
            str(model_path), sess_options=options, providers=["CPUExecutionProvider"]
        )
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        self._tokenizer.enable_padding()

    @property
    def model_key(self) -> str:
        return BGE_SMALL_EN_V15

    @property
    def dims(self) -> int:
        return _BGE_DIMS

    def _encode(self, texts: Sequence[str]) -> list[list[float]]:
        encodings = self._tokenizer.encode_batch(list(texts))
        inputs = {
            "input_ids": np.array([e.ids for e in encodings], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encodings], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encodings], dtype=np.int64),
        }
        outputs = self._session.run(["last_hidden_state"], inputs)
        # CLS pooling: bge represents the sequence in position 0, not in a mean over tokens.
        cls = np.asarray(outputs[0])[:, 0]
        normalized = cls / np.linalg.norm(cls, axis=1, keepdims=True)
        return [[float(value) for value in row] for row in normalized]

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, list(texts)) if texts else []

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await asyncio.to_thread(self._encode, [BGE_QUERY_INSTRUCTION + text])
        return vector


def build_bge_small(threads: int = 0) -> OnnxEmbedder:
    """The default embedder, from the Bazel-pinned weights in runfiles."""
    return OnnxEmbedder(
        runfiles.get_required_path("bge_small_en_v15_model/file/model.onnx"),
        runfiles.get_required_path("bge_small_en_v15_tokenizer/file/tokenizer.json"),
        threads=threads,
    )
