# Model catalogue

Shared Python inputs for Kubernetes and Nix configuration, with no dependency on
cdk8s. Consumers select source models or gateway routes and serialize them at their
own boundary.

- `ollama.py`: source tags, names, audited GGUF context facts and requested serving
  variants. Both Ollama
  provisioning and gateway routing consume these; LiteLLM does not own provisioning.
- `catalog.py`: account-specific model facts, account/wire identities, named routes,
  compatibility aliases, and ordered rosters. Define named routes **before** rosters;
  a tuple's position or a lookup by slug must not define a named selection.
- `policies.py`: shared client lanes. Each lane has its
  allowed routes and ordered fallback targets together. Authorization, offering a
  model in a picker, and choosing defaults are different decisions.
- `nix.py`: wrapper choices and their model-only JSON projection for Nix Claude Code wrappers.
  Regenerate `claude-wrappers.json` with `bb run //model_catalog:generate_nix`.

The ChatGPT subscription account uses the `chatgpt-sub/` route prefix. LiteLLM
interprets `chatgpt/` as its built-in provider during request preprocessing,
before resolving configured routes; that prefix can initiate interactive login
and block the proxy event loop.

Account means whose credentials/account serve a model, not its manufacturer. Shape
means the outbound wire, not the client's endpoint. Unknown facts remain unknown;
comments retain the provenance of published, measured, or configured limits. A
configured context or client override is not evidence of model capacity. Generative
metadata is a `TokenLimits` pair; embeddings use input-only `EmbeddingLimits`.
Unknown limits remain absent; see the
[semantics and remaining provenance gaps](design.md#current-token-limit-shape).

Cluster endpoints, credential references, and environment-specific selections stay
in `cluster/cdk8s`. LiteLLM binds the catalogue's `Upstream` values to deployment
settings in `cluster/cdk8s/litellm/upstreams.py`. Importing this catalogue does not
make Kubernetes generation internals public, or establish deployed availability or
authorization. Runtime services consume their own serialized config schemas; live
probes and acceptance checks inspect deployed APIs or committed artifacts.

See [consumer wiring](../cluster/docs/model_catalog.md) for projections and checks.

## Refactor documentation

- [Design](design.md): goals, ownership, example wiring, rollout and shared vocabulary.
- [Migration inventory](migration_inventory.md): settled boundaries and remaining
  file/consumer disposition decisions.
- [Routing review](routing_review.md) and [options/source audit](routing_options.md):
  routing-first requirements, candidate features and acceptance gaps.
- Supporting research: [LiteLLM metadata](litellm_metadata.md),
  [Antigravity limits](antigravity_limits.md), and [client budgets](client_budgets.md).
- [Tracking issue #9574](https://github.com/agentydragon/ducktape/issues/9574): PR/rollout
  status and parked integrations, including restoration obligations.
