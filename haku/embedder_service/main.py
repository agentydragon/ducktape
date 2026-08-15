"""An OpenAI-compatible embedding endpoint over the pinned bge-small weights.

Embedding is the one CPU-heavy thing the index does, and it is the one piece expected to be
replaced — by Ollama, or by anything else that speaks `/v1/embeddings`. Both of those argue for
the same shape: a small service behind the standard wire format, so consumers hold an
`AsyncOpenAI` client and moving off this is a base URL.

**Deliberately not smart.** No batching queue, no caching, no auth: the index's own `chunks` table
is already the embedding cache, callers already batch, and admission is the cluster's job (this
listens in-cluster only). What it owns is the model and the CPU it burns.

**The query instruction is not applied here.** bge's asymmetry is model knowledge, and this
endpoint is model-generic by construction — a client that prefixes queries itself works
identically against Ollama, which would not know to do it either.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

import typer
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from haku.state_index.embedder import OnnxEmbedder, build_bge_small

logger = logging.getLogger(__name__)

# One request's worth of inputs. A caller batching more than this is asking for a multi-megabyte
# body and a request that outlives its own timeout; the index's own batches are far smaller.
MAX_INPUTS = 256


class EmbeddingsRequest(BaseModel):
    model: str
    input: str | list[str]


class Embedding(BaseModel):
    object: Literal["embedding"] = "embedding"
    index: int
    embedding: list[float]


class Usage(BaseModel):
    """Present because the client library's response model requires it; nothing here bills."""

    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbeddingsResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[Embedding]
    model: str = Field(description="What actually computed these — a client compares it to what it asked for.")
    usage: Usage = Usage()


def build_app(embedder: OnnxEmbedder) -> FastAPI:
    app = FastAPI(title="haku embedder")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "model": embedder.model_key}

    @app.post("/v1/embeddings")
    async def embeddings(request: EmbeddingsRequest) -> EmbeddingsResponse:
        if request.model != embedder.model_key:
            # 404, as OpenAI answers for a model it does not serve — a client's mismatch check
            # should read as "wrong model", not as this server being broken.
            raise HTTPException(status_code=404, detail=f"model {request.model!r} is not served here")
        inputs = [request.input] if isinstance(request.input, str) else request.input
        if len(inputs) > MAX_INPUTS:
            raise HTTPException(status_code=413, detail=f"at most {MAX_INPUTS} inputs per request")
        vectors = await embedder.embed_documents(inputs)
        return EmbeddingsResponse(
            data=[Embedding(index=index, embedding=vector) for index, vector in enumerate(vectors)],
            model=embedder.model_key,
        )

    return app


cli = typer.Typer(help=__doc__)


@cli.command()
def serve(
    host: str = "0.0.0.0",
    port: int = 8080,
    threads: Annotated[int, typer.Option(help="onnxruntime intra-op threads; set to the pod's CPU limit.")] = 0,
) -> None:
    """Serve embeddings until killed."""
    logging.basicConfig(level=logging.INFO)
    # Loaded before the server starts rather than on first request: a pod that is Ready should be
    # able to answer, and the load is seconds of CPU.
    embedder = build_bge_small(threads=threads)
    logger.info("serving %s", embedder.model_key)
    uvicorn.run(build_app(embedder), host=host, port=port)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
