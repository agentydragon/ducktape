"""Build the Langfuse OTLP auth header before starting LiteLLM."""

from __future__ import annotations

import base64
import os
import sys


def langfuse_otlp_headers(public_key: str, secret_key: str) -> str:
    """Return OTLP headers for Langfuse's v4 ingestion endpoint."""
    if not public_key or not secret_key:
        raise ValueError("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required for OTLP export")
    credentials = base64.b64encode(f"{public_key}:{secret_key}".encode("utf-8")).decode("ascii")
    return f"Authorization=Basic {credentials},x-langfuse-ingestion-version=4"


def main() -> None:
    os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = langfuse_otlp_headers(
        os.environ.get("LANGFUSE_PUBLIC_KEY", ""), os.environ.get("LANGFUSE_SECRET_KEY", "")
    )
    command = ["litellm", *sys.argv[1:]]
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
