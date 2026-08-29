"""Deploy-owned recall-index configuration: the embedder endpoint and the logical-index registry.

Shared by the console's query-time readers and the haku-indexer worker's maintenance stages, so
one declaration keeps both on the same registry, credential slots, and chunk budget.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, model_validator

from haku.recall_index.chunking import DEFAULT_CHUNK_BUDGET, ChunkBudget
from util.env import EnvironmentVariableName


class EmbedderConfig(BaseModel):
    """Where the `haku_index` tools compute embeddings: any OpenAI-compatible `/v1/embeddings`.

    Ollama today, LiteLLM or anything else that speaks the format tomorrow — which is why this is
    a URL and a model name rather than a backend choice.

    `model` is also the index's `model_key`, so it names the model and not the deployment: point
    it at a different server serving the same model and every cached vector is still valid; point
    it at a different model and the cache misses by construction rather than by anyone noticing.
    """

    base_url: str = Field(description="Base URL including the API version, e.g. http://haku-embedder:8080/v1")
    model: str
    # Instruction-aware models want queries prefixed and documents plain (Qwen3-Embedding, bge,
    # E5). It belongs to the model rather than to the endpoint, so it is configured beside the
    # model name; a model without that asymmetry leaves it empty.
    query_instruction: str = ""
    # The client library requires one; Ollama ignores it, a hosted endpoint would not.
    api_key: SecretStr = SecretStr("not-used")
    # Explicit because the client library's default is ten minutes, and this sits on the search
    # request path: a slow embedder should fail a search, not hold a connection until the caller
    # gives up. Generous enough for a cold model load, short enough to be an error rather than a
    # hang — and it wants to be, since Ollama is a zone away from this pod.
    timeout_seconds: float = Field(default=30.0, gt=0.0)
    # The sync sweeps embed batches of documents off the request path, where waiting out a cold
    # model load is what you want and giving up means the corpus simply never fills.
    sync_timeout_seconds: float = Field(default=300.0, gt=0.0)


class RecallIndexSettings(BaseModel):
    """Retrieval-unit sizing shared by the console's index writers and readers.

    The same complete budget must reach both paths: it is serialized into ``chunker_key``, so a
    reader under another budget would search a regime the writers never produced.
    """

    chunk_budget: ChunkBudget = Field(default=DEFAULT_CHUNK_BUDGET)


class RecallIndexDefinition(BaseModel):
    """One configured logical index.

    The configuration, rather than an implicit name convention, is the authority for what this
    deployment indexes. ``index_id`` is the durable retrieval and future grant boundary; the
    index type's configuration describes its upstream directly.
    """

    index_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9-]*$")


class GitRecallIndexDefinition(RecallIndexDefinition):
    """A Git logical index, fetched into an untrusted disposable bare mirror."""

    index_type: Literal["git"] = "git"
    repo_url: str
    branch: str = "main"
    username_env_var: EnvironmentVariableName | None = None
    password_env_var: EnvironmentVariableName | None = None
    # A bare mirror, on ephemeral pod storage by default: losing it costs a clone, not an
    # embedding, since the chunk cache is content-addressed and lives in Postgres.
    mirror_path: Path = Path("/tmp/haku-recall-index/mirror.git")

    @model_validator(mode="after")
    def _require_complete_credentials(self) -> GitRecallIndexDefinition:
        if (self.username_env_var is None) != (self.password_env_var is None):
            raise ValueError("Git recall index credentials require both username_env_var and password_env_var")
        return self


class ChatRecallIndexDefinition(RecallIndexDefinition):
    """A logical index over this console's completed chat-message source."""

    index_type: Literal["chat"] = "chat"


type ConfiguredRecallIndex = Annotated[
    GitRecallIndexDefinition | ChatRecallIndexDefinition, Field(discriminator="index_type")
]
