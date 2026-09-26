"""The per-key model allowlists tf/gitops/litellm-keys/main.tf scopes its LiteLLM virtual
keys to, derived from model_rosters.py and handed to the module as the `model_allowlists`
variable of its generated Terraform CR (generate_manifests.py). Every name is checked
against what the main proxy serves before it is written.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import terraform
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.litellm.config import main_proxy_config
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.model_rosters import (
    ANTHROPIC_MODELS,
    CLIPROXY_MODELS,
    GEMINI_EMBEDDING_COMPAT_ALIAS,
    GEMINI_EMBEDDING_MODELS,
    GEMINI_MODELS,
    MISTRAL_MODELS,
    OLLAMA_CHAT_MODELS,
    OLLAMA_EMBEDDING_MODEL,
    TANA_MODELS,
    ApiShape,
    Provider,
    codex_messages_name,
    codex_responses_name,
    exposed_name,
    ollama_chat_variant,
)

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/litellm/keys-tf"

# The Codex-subscription models on LiteLLM's Responses surface, for Codex CLI clients
# (codex-pod, agent-workspaces-codex, the agentplane staging session form) -- served
# by CLIProxyAPI.
OAI_LANE_MODELS = [codex_responses_name(model) for model in CLIPROXY_MODELS]
# The same models on the Anthropic Messages surface -- Claude Code clients
# (laptop codex-claude, agent-box, codex-pod).
CODEX_CLIENT_MODELS = [codex_messages_name(model) for model in CLIPROXY_MODELS]
# Claude-subscription models on the Anthropic Messages surface, fronted through
# CLIProxyAPI's Claude OAuth session -- the laptop litellm-claude wrapper and the
# agentplane staging session form. A different upstream session on the same pod as the
# Codex lanes; distinct from the direct-API anthropic-api/ant-messages/* entries.
CLAUDE_CLIENT_MODELS = [
    exposed_name(Provider.ANTHROPIC_MAX20, ApiShape.ANT_MESSAGES, model) for model in ANTHROPIC_MODELS
]
# Tana-UI models served by the main proxy's in-process Tana provider -- the laptop
# tana-claude wrapper.
TANA_CLIENT_MODELS = [exposed_name(Provider.TANA, ApiShape.ANT_MESSAGES, exposed) for exposed, _ in TANA_MODELS]
# The Gemini chat lineup -- the laptop gemini-claude wrapper and public-coder-agent.
GEMINI_CLIENT_MODELS = [exposed_name(Provider.GOOGLE, ApiShape.GOOG_GENERATE, model.id) for model in GEMINI_MODELS]
_GEMINI_EMBEDDING_ROUTES = [
    exposed_name(Provider.GOOGLE, ApiShape.GOOG_EMBED, model) for model in GEMINI_EMBEDDING_MODELS
]
# Embeddings for agents whose egress cannot reach api.openai.com: a domain-confined
# agent has no route to a direct OpenAI Platform key and should not gain one just to
# embed, so its OpenClaw memory index rides the in-cluster path it already uses for
# turns.
EMBEDDING_CLIENT_MODELS = [
    GEMINI_EMBEDDING_COMPAT_ALIAS,
    *_GEMINI_EMBEDDING_ROUTES,
    exposed_name(Provider.OLLAMA, ApiShape.OLM_EMBED, OLLAMA_EMBEDDING_MODEL),
]

# Both existing LiteLLM chat routes for each local Ollama model, exposed to Agentplane
# through the environment's own key.
OLLAMA_CHAT_CLIENT_MODELS = [
    exposed_name(Provider.OLLAMA, shape, ollama_chat_variant(model, context))
    for model, _, contexts in OLLAMA_CHAT_MODELS
    for context in contexts
    for shape in (ApiShape.OAI_CHAT, ApiShape.OLM_CHAT)
]

# The one native subscription model per harness the agentplane testing session form
# offers; the Codex one on both wires, for Claude Code clients on the same key.
CHEAP_EXPERIMENTS_CLAUDE_MODEL = exposed_name(
    Provider.ANTHROPIC_API, ApiShape.ANT_MESSAGES, "claude-haiku-4-5-20251001"
)
_CHEAP_EXPERIMENTS_CODEX = "gpt-6-luna"
CHEAP_EXPERIMENTS_CODEX_MODEL = codex_responses_name(_CHEAP_EXPERIMENTS_CODEX)
# The cheap-experiments key, shared with agents only through an expiring Haku Console
# Kubernetes grant and standing on the agentplane testing LLM ingress. Intentionally an
# exact, cheap-model-only set rather than a provider-wide prefix or wildcard: the Gemini
# chat and embedding lineups, the API-key-verified Mistral chat roster, every
# model/context/protocol variant of the self-hosted Ollama chat models, and the two
# native subscription models above.
CHEAP_EXPERIMENTS_MODELS = [
    *GEMINI_CLIENT_MODELS,
    *_GEMINI_EMBEDDING_ROUTES,
    *(exposed_name(Provider.MISTRAL, ApiShape.OAI_CHAT, model) for model in MISTRAL_MODELS),
    *OLLAMA_CHAT_CLIENT_MODELS,
    CHEAP_EXPERIMENTS_CLAUDE_MODEL,
    codex_messages_name(_CHEAP_EXPERIMENTS_CODEX),
    CHEAP_EXPERIMENTS_CODEX_MODEL,
]


def model_allowlists() -> dict[str, list[str]]:
    """The lanes keyed as main.tf's `var.model_allowlists` reads them."""
    served = {entry["model_name"] for entry in main_proxy_config()["model_list"]}
    lanes = {
        "oai_lane_models": OAI_LANE_MODELS,
        "tana_client_models": TANA_CLIENT_MODELS,
        "codex_client_models": CODEX_CLIENT_MODELS,
        "claude_client_models": CLAUDE_CLIENT_MODELS,
        "embedding_client_models": EMBEDDING_CLIENT_MODELS,
        "gemini_client_models": GEMINI_CLIENT_MODELS,
        "ollama_chat_client_models": OLLAMA_CHAT_CLIENT_MODELS,
        "cheap_experiments_models": CHEAP_EXPERIMENTS_MODELS,
    }
    for lane, models in lanes.items():
        unserved = [model for model in models if model not in served]
        if unserved:
            raise ValueError(f"{lane=} allowlists models the proxy does not serve: {unserved}")
    return lanes


def keys_chart(app: App) -> Chart:
    """Mints the agent and laptop-client LiteLLM virtual keys (tf/gitops/litellm-keys).
    Needs the SOPS-managed master key and a serving LiteLLM with its virtual-key DB;
    tofu-controller retries on its interval until LiteLLM is up.
    """
    chart = Chart(app, "litellm-keys", disable_resource_name_hashes=True)
    terraform.gitops_terraform(
        chart,
        "terraform",
        name="litellm-keys",
        variables={"model_allowlists": model_allowlists()},
        env=[
            # The narrow SOPS age private key (litellm-clients-sops-age-key.sops.yaml
            # beside this CR) that decrypts the module's pinned client-key files for
            # its `sops_file` data sources -- single-purpose, not the broad cluster key.
            terraform.secret_env("SOPS_AGE_KEY", "litellm-clients-sops-age-key", "key")
        ],
    )
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, keys_chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=["litellm-keys.k8s.yaml", "litellm-clients-sops-age-key.sops.yaml"]),
    )


# The litellm-keys Terraform CR lives DOWNSTREAM of the litellm app, not in
# litellm-secrets: minting virtual keys needs a serving LiteLLM with its
# virtual-key DB. Coupling the TF's health into litellm-secrets (the app's
# dependency) deadlocked the 2026-07-02 rollout — the app never applied the
# DATABASE_URL deployment because its secrets layer waited on a TF apply that
# needed the app. Dependency direction here is the fix.
def litellm_keys_tf(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    litellm: Kustomization,
    tofu_controller: Kustomization,
    tofu_state_db: Kustomization,
) -> Kustomization:
    name = "litellm-keys-tf"
    return flux_kustomization(
        chart,
        name,
        artifact,
        timeout="10m",
        # Decrypt litellm-clients-sops-age-key.sops.yaml (the narrow SOPS_AGE_KEY for
        # the tf-runner) so sops_file in tf/gitops/litellm-keys can read the virtual-key
        # SSOT. Added when that SOPS file arrived — previously this dir held only plain YAML.
        decryption=SOPS_DECRYPTION,
        depends_on=flux_kustomization_depends_on_many(
            # The app must serve (with its DB) before keys can mint.
            litellm,
            tofu_controller,
            tofu_state_db,
        ),
    )
