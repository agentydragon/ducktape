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

from typing import Final

from pydantic import BaseModel


class ModelMetadata(BaseModel):
    """Complete metadata for an OpenAI model."""

    input_usd_per_1m_tokens: float
    cached_input_usd_per_1m_tokens: float  # Equals input price if no cache discount
    output_usd_per_1m_tokens: float
    context_window_tokens: int
    max_output_tokens: int


# Comprehensive model metadata combining pricing + context limits
# Context limits are based on web sources (OpenAI does not publish machine-readable limits)
MODEL_METADATA: Final[dict[str, ModelMetadata]] = {
    # GPT-5 family (400k context per web sources, 128k max output)
    "gpt-5.1": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.25,
        cached_input_usd_per_1m_tokens=0.025,
        output_usd_per_1m_tokens=2.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-nano": ModelMetadata(
        input_usd_per_1m_tokens=0.05,
        cached_input_usd_per_1m_tokens=0.005,
        output_usd_per_1m_tokens=0.40,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5.1-chat-latest": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-chat-latest": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5.1-codex": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5.1-codex-max": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-codex": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    "gpt-5-pro": ModelMetadata(
        input_usd_per_1m_tokens=15.00,
        cached_input_usd_per_1m_tokens=15.00,  # No cache discount
        output_usd_per_1m_tokens=120.00,
        context_window_tokens=400_000,
        max_output_tokens=128_000,
    ),
    # GPT-4.1 family (128k context, 16k output per web sources)
    "gpt-4.1": ModelMetadata(
        input_usd_per_1m_tokens=2.00,
        cached_input_usd_per_1m_tokens=0.50,
        output_usd_per_1m_tokens=8.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4.1-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.40,
        cached_input_usd_per_1m_tokens=0.10,
        output_usd_per_1m_tokens=1.60,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4.1-nano": ModelMetadata(
        input_usd_per_1m_tokens=0.10,
        cached_input_usd_per_1m_tokens=0.025,
        output_usd_per_1m_tokens=0.40,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # GPT-4o family (128k context, 16k output)
    "gpt-4o": ModelMetadata(
        input_usd_per_1m_tokens=2.50,
        cached_input_usd_per_1m_tokens=1.25,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-2024-05-13": ModelMetadata(
        input_usd_per_1m_tokens=5.00,
        cached_input_usd_per_1m_tokens=5.00,  # No cache discount
        output_usd_per_1m_tokens=15.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.15,
        cached_input_usd_per_1m_tokens=0.075,
        output_usd_per_1m_tokens=0.60,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # Realtime models (128k context, 4k output)
    "gpt-realtime": ModelMetadata(
        input_usd_per_1m_tokens=4.00,
        cached_input_usd_per_1m_tokens=0.40,
        output_usd_per_1m_tokens=16.00,
        context_window_tokens=128_000,
        max_output_tokens=4_096,
    ),
    "gpt-realtime-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.60,
        cached_input_usd_per_1m_tokens=0.06,
        output_usd_per_1m_tokens=2.40,
        context_window_tokens=128_000,
        max_output_tokens=4_096,
    ),
    "gpt-4o-realtime-preview": ModelMetadata(
        input_usd_per_1m_tokens=5.00,
        cached_input_usd_per_1m_tokens=2.50,
        output_usd_per_1m_tokens=20.00,
        context_window_tokens=128_000,
        max_output_tokens=4_096,
    ),
    "gpt-4o-mini-realtime-preview": ModelMetadata(
        input_usd_per_1m_tokens=0.60,
        cached_input_usd_per_1m_tokens=0.30,
        output_usd_per_1m_tokens=2.40,
        context_window_tokens=128_000,
        max_output_tokens=4_096,
    ),
    # Audio models (128k context, 16k output except mini variants)
    "gpt-audio": ModelMetadata(
        input_usd_per_1m_tokens=2.50,
        cached_input_usd_per_1m_tokens=2.50,  # No cache discount
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-audio-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.60,
        cached_input_usd_per_1m_tokens=0.60,  # No cache discount
        output_usd_per_1m_tokens=2.40,
        context_window_tokens=128_000,
        max_output_tokens=4_096,
    ),
    "gpt-4o-audio-preview": ModelMetadata(
        input_usd_per_1m_tokens=2.50,
        cached_input_usd_per_1m_tokens=2.50,  # No cache discount
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-mini-audio-preview": ModelMetadata(
        input_usd_per_1m_tokens=0.15,
        cached_input_usd_per_1m_tokens=0.15,  # No cache discount
        output_usd_per_1m_tokens=0.60,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # O1 reasoning models (200k context, 100k output)
    "o1": ModelMetadata(
        input_usd_per_1m_tokens=15.00,
        cached_input_usd_per_1m_tokens=7.50,
        output_usd_per_1m_tokens=60.00,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    "o1-pro": ModelMetadata(
        input_usd_per_1m_tokens=150.00,
        cached_input_usd_per_1m_tokens=150.00,  # No cache discount
        output_usd_per_1m_tokens=600.00,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    "o1-mini": ModelMetadata(
        input_usd_per_1m_tokens=1.10,
        cached_input_usd_per_1m_tokens=0.55,
        output_usd_per_1m_tokens=4.40,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    # O3 reasoning models (o3/o3-mini: 128k context; o3-pro: 200k; all 100k output)
    "o3": ModelMetadata(
        input_usd_per_1m_tokens=2.00,
        cached_input_usd_per_1m_tokens=0.50,
        output_usd_per_1m_tokens=8.00,
        context_window_tokens=128_000,
        max_output_tokens=100_000,
    ),
    "o3-pro": ModelMetadata(
        input_usd_per_1m_tokens=20.00,
        cached_input_usd_per_1m_tokens=20.00,  # No cache discount
        output_usd_per_1m_tokens=80.00,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    "o3-mini": ModelMetadata(
        input_usd_per_1m_tokens=1.10,
        cached_input_usd_per_1m_tokens=0.55,
        output_usd_per_1m_tokens=4.40,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    "o3-deep-research": ModelMetadata(
        input_usd_per_1m_tokens=10.00,
        cached_input_usd_per_1m_tokens=2.50,
        output_usd_per_1m_tokens=40.00,
        context_window_tokens=200_000,
        max_output_tokens=100_000,
    ),
    # O4 models (128k context, 100k output)
    "o4-mini": ModelMetadata(
        input_usd_per_1m_tokens=1.10,
        cached_input_usd_per_1m_tokens=0.275,
        output_usd_per_1m_tokens=4.40,
        context_window_tokens=128_000,
        max_output_tokens=100_000,
    ),
    "o4-mini-deep-research": ModelMetadata(
        input_usd_per_1m_tokens=2.00,
        cached_input_usd_per_1m_tokens=0.50,
        output_usd_per_1m_tokens=8.00,
        context_window_tokens=128_000,
        max_output_tokens=100_000,
    ),
    # Codex models (128k context, 16k output)
    "gpt-5.1-codex-mini": ModelMetadata(
        input_usd_per_1m_tokens=0.25,
        cached_input_usd_per_1m_tokens=0.025,
        output_usd_per_1m_tokens=2.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "codex-mini-latest": ModelMetadata(
        input_usd_per_1m_tokens=1.50,
        cached_input_usd_per_1m_tokens=0.375,
        output_usd_per_1m_tokens=6.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # Search models (128k context, 16k output)
    "gpt-5-search-api": ModelMetadata(
        input_usd_per_1m_tokens=1.25,
        cached_input_usd_per_1m_tokens=0.125,
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-mini-search-preview": ModelMetadata(
        input_usd_per_1m_tokens=0.15,
        cached_input_usd_per_1m_tokens=0.15,  # No cache discount
        output_usd_per_1m_tokens=0.60,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    "gpt-4o-search-preview": ModelMetadata(
        input_usd_per_1m_tokens=2.50,
        cached_input_usd_per_1m_tokens=2.50,  # No cache discount
        output_usd_per_1m_tokens=10.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # Computer use (128k context, 16k output)
    "computer-use-preview": ModelMetadata(
        input_usd_per_1m_tokens=3.00,
        cached_input_usd_per_1m_tokens=3.00,  # No cache discount
        output_usd_per_1m_tokens=12.00,
        context_window_tokens=128_000,
        max_output_tokens=16_384,
    ),
    # Image models (128k context, no text output)
    "gpt-image-1": ModelMetadata(
        input_usd_per_1m_tokens=5.00,
        cached_input_usd_per_1m_tokens=1.25,
        output_usd_per_1m_tokens=0.0,
        context_window_tokens=128_000,
        max_output_tokens=0,
    ),
    "gpt-image-1-mini": ModelMetadata(
        input_usd_per_1m_tokens=2.00,
        cached_input_usd_per_1m_tokens=0.20,
        output_usd_per_1m_tokens=0.0,
        context_window_tokens=128_000,
        max_output_tokens=0,
    ),
}


def get_model_metadata(model_id: str | None) -> ModelMetadata:
    """Get complete metadata for a model.

    Raises KeyError if model_id is None, empty, or unknown.
    """
    if not model_id:
        raise KeyError("model_id cannot be None or empty")

    if model_id not in MODEL_METADATA:
        raise KeyError(f"Unknown model: {model_id!r}. Available models: {', '.join(sorted(MODEL_METADATA.keys()))}")

    return MODEL_METADATA[model_id]
