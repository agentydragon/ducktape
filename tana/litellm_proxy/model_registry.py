from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

TanaModelProvider = Literal["openai", "anthropic", "gemini"]


@dataclass(frozen=True)
class TanaLlmModel:
    key: str
    model_id: str
    provider: TanaModelProvider


# Source: gaffer-private/tana/re/web/78d928dca7/js/index-DI2GynTv/domains/system/ids.js
# `supportedLlmModelIds = Object.values(llmModelIds)`, where `llmModelIds`
# spreads these OpenAI, Anthropic, and Gemini maps.
TANA_LLM_MODELS: tuple[TanaLlmModel, ...] = (
    TanaLlmModel("gpt35turbo", "gpt-3.5-turbo-0125", "openai"),
    TanaLlmModel("gpt35turbo_instruct", "gpt-3.5-turbo-instruct", "openai"),
    TanaLlmModel("gpt4turbo_128k", "gpt-4-turbo", "openai"),
    TanaLlmModel("gpt4turbo_128k_2024_04_09", "gpt-4-turbo-2024-04-09", "openai"),
    TanaLlmModel("gpt4turbo_128k_preview", "gpt-4-turbo-preview", "openai"),
    TanaLlmModel("gpt4turbo_128k_0125_preview", "gpt-4-0125-preview", "openai"),
    TanaLlmModel("gpt4_8k", "gpt-4", "openai"),
    TanaLlmModel("gpt4_32k", "gpt-4-32k", "openai"),
    TanaLlmModel("gpt4o", "gpt-4o", "openai"),
    TanaLlmModel("gpt4o_mini", "gpt-4o-mini", "openai"),
    TanaLlmModel("gpt4o_2024_05_13", "gpt-4o-2024-05-13", "openai"),
    TanaLlmModel("gpt4o_2024_11_20", "gpt-4o-2024-11-20", "openai"),
    TanaLlmModel("o3_mini", "o3-mini", "openai"),
    TanaLlmModel("o1", "o1", "openai"),
    TanaLlmModel("o1_2024_12_17", "o1-2024-12-17", "openai"),
    TanaLlmModel("o1_preview", "o1-preview", "openai"),
    TanaLlmModel("o1_preview_2024_09_12", "o1-preview-2024-09-12", "openai"),
    TanaLlmModel("gpt45_preview", "gpt-4.5-preview", "openai"),
    TanaLlmModel("gpt45_preview_2025_02_27", "gpt-4.5-preview-2025-02-27", "openai"),
    TanaLlmModel("o4_mini", "o4-mini", "openai"),
    TanaLlmModel("o4_mini_2025_04_16", "o4-mini-2025-04-16", "openai"),
    TanaLlmModel("o3", "o3", "openai"),
    TanaLlmModel("o3_2025_04_16", "o3-2025-04-16", "openai"),
    TanaLlmModel("o3_pro", "o3-pro", "openai"),
    TanaLlmModel("o3_pro_2025_06_10", "o3-pro-2025-06-10", "openai"),
    TanaLlmModel("gpt_41", "gpt-4.1", "openai"),
    TanaLlmModel("gpt_41_2025_04_14", "gpt-4.1-2025-04-14", "openai"),
    TanaLlmModel("gpt_41_mini", "gpt-4.1-mini", "openai"),
    TanaLlmModel("gpt_41_mini_2025_04_14", "gpt-4.1-mini-2025-04-14", "openai"),
    TanaLlmModel("gpt_41_nano", "gpt-4.1-nano", "openai"),
    TanaLlmModel("gpt_41_nano_2025_04_14", "gpt-4.1-nano-2025-04-14", "openai"),
    TanaLlmModel("gpt5", "gpt-5", "openai"),
    TanaLlmModel("gpt5_mini", "gpt-5-mini", "openai"),
    TanaLlmModel("gpt5_nano", "gpt-5-nano", "openai"),
    TanaLlmModel("gpt51_minimal", "gpt-5.1/none", "openai"),
    TanaLlmModel("gpt51_low", "gpt-5.1/low", "openai"),
    TanaLlmModel("gpt51_medium", "gpt-5.1/medium", "openai"),
    TanaLlmModel("gpt51_high", "gpt-5.1/high", "openai"),
    TanaLlmModel("gpt51_extended", "gpt-5.1/extended", "openai"),
    TanaLlmModel("gpt52_minimal", "gpt-5.2/none", "openai"),
    TanaLlmModel("gpt52_low", "gpt-5.2/low", "openai"),
    TanaLlmModel("gpt52_medium", "gpt-5.2/medium", "openai"),
    TanaLlmModel("gpt52_high", "gpt-5.2/high", "openai"),
    TanaLlmModel("gpt52_xhigh", "gpt-5.2/xhigh", "openai"),
    TanaLlmModel("gpt54_minimal", "gpt-5.4/none", "openai"),
    TanaLlmModel("gpt54_low", "gpt-5.4/low", "openai"),
    TanaLlmModel("gpt54_medium", "gpt-5.4/medium", "openai"),
    TanaLlmModel("gpt54_high", "gpt-5.4/high", "openai"),
    TanaLlmModel("gpt54_xhigh", "gpt-5.4/xhigh", "openai"),
    TanaLlmModel("claude_3_5_sonnet", "claude-3-5-sonnet-latest", "anthropic"),
    TanaLlmModel("claude_3_7_sonnet", "claude-3-7-sonnet-20250219", "anthropic"),
    TanaLlmModel("claude_3_7_sonnet_extended", "claude-3-7-sonnet-20250219/extended", "anthropic"),
    TanaLlmModel("claude_sonnet_4", "claude-sonnet-4-20250514", "anthropic"),
    TanaLlmModel("claude_sonnet_4_extended", "claude-sonnet-4-20250514/extended", "anthropic"),
    TanaLlmModel("claude_sonnet_4_5", "claude-sonnet-4-5-20250929", "anthropic"),
    TanaLlmModel("claude_haiku_4_5", "claude-haiku-4-5-20251001", "anthropic"),
    TanaLlmModel("claude_opus_4_1", "claude-opus-4-1-20250805", "anthropic"),
    TanaLlmModel("claude_opus_4_5_low", "claude-opus-4-5-20251101/low", "anthropic"),
    TanaLlmModel("claude_opus_4_5_medium", "claude-opus-4-5-20251101/medium", "anthropic"),
    TanaLlmModel("claude_opus_4_5_high", "claude-opus-4-5-20251101/high", "anthropic"),
    TanaLlmModel("claude_sonnet_4_6_low", "claude-sonnet-4-6/low", "anthropic"),
    TanaLlmModel("claude_sonnet_4_6_medium", "claude-sonnet-4-6/medium", "anthropic"),
    TanaLlmModel("claude_sonnet_4_6_high", "claude-sonnet-4-6/high", "anthropic"),
    TanaLlmModel("claude_opus_4_6_low", "claude-opus-4-6/low", "anthropic"),
    TanaLlmModel("claude_opus_4_6_medium", "claude-opus-4-6/medium", "anthropic"),
    TanaLlmModel("claude_opus_4_6_high", "claude-opus-4-6/high", "anthropic"),
    TanaLlmModel("gemini2_flash", "gemini-2.0-flash", "gemini"),
    TanaLlmModel("gemini2_flash_lite", "gemini-2.0-flash-lite-preview-02-05", "gemini"),
    TanaLlmModel("gemini2_5_flash_preview", "gemini-2.5-flash-preview-04-17", "gemini"),
    TanaLlmModel("gemini2_5_flash_reasoning_preview", "gemini-2.5-flash-preview-04-17/reasoning", "gemini"),
    TanaLlmModel("gemini2_5_flash", "gemini-2.5-flash", "gemini"),
    TanaLlmModel("gemini2_5_flash_reasoning", "gemini-2.5-flash/reasoning", "gemini"),
    TanaLlmModel("gemini2_5_pro", "gemini-2.5-pro", "gemini"),
    TanaLlmModel("gemini3_pro_preview_low", "gemini-3-pro-preview/low", "gemini"),
    TanaLlmModel("gemini3_pro_preview_high", "gemini-3-pro-preview/high", "gemini"),
    TanaLlmModel("gemini3_pro_image_preview", "gemini-3-pro-image-preview", "gemini"),
    TanaLlmModel("gemini3_flash_preview_low", "gemini-3-flash-preview/low", "gemini"),
    TanaLlmModel("gemini3_flash_preview_high", "gemini-3-flash-preview/high", "gemini"),
    TanaLlmModel("gemini31_pro_preview_low", "gemini-3.1-pro-preview/low", "gemini"),
    TanaLlmModel("gemini31_pro_preview_medium", "gemini-3.1-pro-preview/medium", "gemini"),
    TanaLlmModel("gemini31_pro_preview_high", "gemini-3.1-pro-preview/high", "gemini"),
)

# A 2026-06-10 probe with a one-token `llmProxy` request got HTTP 200 for every RE-listed model above
# except gpt-3.5-turbo-0125, gpt-3.5-turbo-instruct, gpt-4-turbo, gpt-4-turbo-2024-04-09,
# gpt-4-turbo-preview, gpt-4-0125-preview, gpt-4, gpt-4-32k, gpt-4o, gpt-4o-2024-05-13,
# gpt-4o-2024-11-20, gpt-4.1, gpt-4.1-2025-04-14, gpt-5.2/xhigh and gpt-5.4/xhigh, which got Tana-side
# 400/500 responses. `probe_models_bin` reprobes.
