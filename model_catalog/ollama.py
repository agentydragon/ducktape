"""Ollama source declarations, consumed by provisioning and gateway configuration.

Tags identify models in Ollama, not LiteLLM routes. Requested num_ctx allocations
are serving settings, not measured capacities or client compaction budgets.
"""

from dataclasses import dataclass

DEFAULT_NUM_CTX = 128 * 1024


@dataclass(frozen=True)
class Model:
    tag: str
    display_name: str | None = None


@dataclass(frozen=True)
class ChatVariant:
    """A base model with a requested context allocation.

    alias is a separately provisioned tag with num_ctx baked in. Without one,
    requests use the base tag; native options can request num_ctx, but Ollama's
    OpenAI-compatible API ignores those options. Neither case proves capacity.
    """

    model: Model
    num_ctx: int
    alias: str | None = None

    @property
    def tag(self) -> str:
        return self.alias or self.model.tag


QWEN_IQ4XS = Model("qwen3.8-flash-next-iq4xs:latest", "Qwen3.8 Flash Next IQ4_XS")
QWEN_IQ4XS_128K = ChatVariant(QWEN_IQ4XS, 128 * 1024)
# /v1 ignores native options.num_ctx, so provision a separate alias for this size.
QWEN_IQ4XS_256K = ChatVariant(QWEN_IQ4XS, 256 * 1024, "qwen3.8-flash-next-iq4xs-256k:latest")
GPT_OSS_20B = Model("gpt-oss:20b", "GPT-OSS 20B")
GPT_OSS_120B = Model("gpt-oss:120b", "GPT-OSS 120B")
GEMMA4 = Model("gemma4:31b-it-q8_0", "Gemma 4 31B")
QWEN_EMBEDDING = Model("qwen3-embedding:4b")

# qwen3.8-flash-next-q4: 125B-total/6B-active MoE, Unsloth Dynamic UD-Q4_K_XL quant
# (metalspork/qwen3.8-flash-next-ud:UD-Q4_K_XL, 112GB), native 256K context. Disabled
# (2026-09-26): does not fit in wyrm2's combined GPU VRAM (87GB resident vs. ~61GB usable
# across 2x RTX 5090), forcing most MoE-expert weight paging onto the HDD-backed
# `llm-models` PVC; measured 0.056-1.44 tokens/sec generation depending on warm-up state
# (~20-1000x too slow to be usable), on both Ollama 0.34.0 and 0.34.4. Tool-call parsing
# itself works correctly, and the same GGUF served directly via a current llama-server
# build off SSD-backed storage on this same hardware reached ~30 tokens/sec -- so the
# model and hardware are capable, this specific Ollama-on-HDD path is not. Re-enable only
# once served from SSD-backed storage or with the full CPU-resident working set reliably
# page-cache-hot; see agentplane/debug/agentplane_ollama_live_smoke_2026_09_24.md for the
# full investigation.
