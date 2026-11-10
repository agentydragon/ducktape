"""LiteLLM deployment endpoints and credential references, not model metadata."""

from dataclasses import dataclass

from model_catalog.catalog import (
    ANTHROPIC_API,
    ANTHROPIC_SUBSCRIPTION,
    ANTIGRAVITY_MESSAGES,
    CHATGPT_MESSAGES,
    CHATGPT_RESPONSES,
    GOOGLE_EMBED,
    GOOGLE_GENERATE,
    GROQ_CHAT,
    GROQ_TRANSCRIBE,
    MISTRAL_CHAT,
    OLLAMA_EMBED,
    OLLAMA_NATIVE,
    OLLAMA_OPENAI,
    RUGGED_LOCAL_OPENAI,
    TANA_CHAT,
    Upstream,
)


@dataclass(frozen=True)
class UpstreamBinding:
    api_base: str | None = None
    api_key: str | None = None


_OLLAMA_BASE = "http://ollama.ollama.svc.cluster.local:11434"
# Emit the rugged endpoint and bearer-token wiring for cluster access.
RUGGED_LOCAL_LLM_ENABLED = True
RUGGED_LOCAL_LLM_SERVICE = "rugged-local-llm"
RUGGED_LOCAL_LLM_SECRET = "rugged-local-llm-bearer-token"
RUGGED_LOCAL_LLM_PORT = 18080  # must match rugged's NixOS bearer proxy listener
RUGGED_LOCAL_LLM_API_BASE = f"http://{RUGGED_LOCAL_LLM_SERVICE}.litellm.svc.cluster.local:{RUGGED_LOCAL_LLM_PORT}/v1"
RUGGED_LOCAL_LLM_KEY_ENV = "RUGGED_LOCAL_LLM_KEY"
_CLIPROXY_BASE = "http://cli-proxy-api.cli-proxy-api.svc.cluster.local:8317"

UPSTREAM_BINDINGS: dict[Upstream, UpstreamBinding] = {
    CHATGPT_MESSAGES: UpstreamBinding(api_base=_CLIPROXY_BASE, api_key="os.environ/CLIPROXY_CLIENT_KEY"),
    CHATGPT_RESPONSES: UpstreamBinding(api_base=f"{_CLIPROXY_BASE}/v1", api_key="os.environ/CLIPROXY_CLIENT_KEY"),
    ANTHROPIC_SUBSCRIPTION: UpstreamBinding(api_base=_CLIPROXY_BASE, api_key="os.environ/CLIPROXY_CLIENT_KEY"),
    ANTHROPIC_API: UpstreamBinding(api_base=None, api_key="os.environ/ANTHROPIC_API_KEY"),
    ANTIGRAVITY_MESSAGES: UpstreamBinding(api_base=_CLIPROXY_BASE, api_key="os.environ/CLIPROXY_CLIENT_KEY"),
    GROQ_CHAT: UpstreamBinding(api_base=None, api_key="os.environ/GROQ_API_KEY"),
    GROQ_TRANSCRIBE: UpstreamBinding(api_base=None, api_key="os.environ/GROQ_API_KEY"),
    GOOGLE_GENERATE: UpstreamBinding(api_base=None, api_key="os.environ/GEMINI_API_KEY"),
    GOOGLE_EMBED: UpstreamBinding(api_base=None, api_key="os.environ/GEMINI_API_KEY"),
    MISTRAL_CHAT: UpstreamBinding(api_base=None, api_key="os.environ/MISTRAL_API_KEY"),
    OLLAMA_EMBED: UpstreamBinding(api_base=_OLLAMA_BASE, api_key=None),
    TANA_CHAT: UpstreamBinding(
        api_base="https://app.tana.inc/functions", api_key="os.environ/TANA_FIREBASE_REFRESH_TOKEN"
    ),
    OLLAMA_NATIVE: UpstreamBinding(api_base=_OLLAMA_BASE, api_key=None),
    OLLAMA_OPENAI: UpstreamBinding(api_base=f"{_OLLAMA_BASE}/v1", api_key="ollama"),
    RUGGED_LOCAL_OPENAI: UpstreamBinding(
        api_base=RUGGED_LOCAL_LLM_API_BASE, api_key=f"os.environ/{RUGGED_LOCAL_LLM_KEY_ENV}"
    ),
}
