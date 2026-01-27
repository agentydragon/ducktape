"""Unified OpenAI model metadata: pricing, context limits, and capabilities.

Data provenance:
  - Pricing: https://openai.com/api/pricing/ (accessed 2025-11-24)
  - Context limits: Web search 2025-11-30
    - GPT-5: https://allthings.how/gpt-5-context-window-limits-and-usage-in-chatgpt-and-api/
    - General: https://www.scriptbyai.com/token-limit-openai-chatgpt/
    - Community reports: https://community.openai.com/t/huge-gpt-5-documentation-gap-flaw-causing-bugs-input-tokens-exceed-the-configured-limit-of-272-000-tokens/1344734
  - Note: OpenAI API /v1/models endpoint does NOT expose context_window or max_output fields

Last updated: 2025-12-08
"""

from __future__ import annotations

import importlib.resources
from typing import Final

import yaml
from pydantic import BaseModel


class ModelMetadata(BaseModel):
    """Complete metadata for an OpenAI model."""

    input_usd_per_1m_tokens: float
    cached_input_usd_per_1m_tokens: float  # Equals input price if no cache discount
    output_usd_per_1m_tokens: float
    context_window_tokens: int
    max_output_tokens: int


def _load_metadata() -> dict[str, ModelMetadata]:
    """Load model metadata from the bundled ``model_metadata.yaml`` file.

    The YAML file is located in the same package and contains a mapping from
    model ids to the five metadata fields.  On import the data is parsed into a
    dictionary of :class:`ModelMetadata` instances.
    """

    yaml_bytes = importlib.resources.read_binary(__package__, "model_metadata.yaml")
    raw = yaml.safe_load(yaml_bytes.decode("utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError("model_metadata.yaml must contain a mapping")
    return {k: ModelMetadata.parse_obj(v) for k, v in raw.items()}


# Load metadata at import time.
MODEL_METADATA: Final[dict[str, ModelMetadata]] = _load_metadata()


def get_model_metadata(model_id: str | None) -> ModelMetadata:
    """Get complete metadata for a model.

    Raises KeyError if model_id is None, empty, or unknown.
    """
    if not model_id:
        raise KeyError("model_id cannot be None or empty")
    if model_id not in MODEL_METADATA:
        raise KeyError(f"Unknown model: {model_id!r}. Available models: {', '.join(sorted(MODEL_METADATA.keys()))}")
    return MODEL_METADATA[model_id]
