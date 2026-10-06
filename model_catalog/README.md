# Model catalogue

Shared Python inputs for Kubernetes and Nix configuration, with no dependency on
cdk8s. Consumers select source models or gateway routes and serialize them at their
own boundary.

- `ollama.py`: source tags, names and requested serving variants. Both Ollama
  provisioning and gateway routing consume these; LiteLLM does not own provisioning.
- `catalog.py`: account-specific model facts, account/wire identities, named routes,
  compatibility aliases, and ordered rosters. Define named routes **before** rosters;
  a tuple's position or a lookup by slug must not define a named selection.
- `policies.py`: shared client lanes. Each lane has its
  allowed routes and ordered fallback targets together. Authorization, offering a
  model in a picker, and choosing defaults are different decisions.
- `nix.py`: wrapper choices and their model-only JSON projection for Nix Claude Code wrappers.
  Regenerate `claude-wrappers.json` with `bb run //model_catalog:generate_nix`.

Account means whose credentials/account serve a model, not its manufacturer. Shape
means the outbound wire, not the client's endpoint. Unknown facts remain unknown;
comments retain the provenance of published, measured, or configured limits. A
configured context or client override is not evidence of model capacity. Input/output
metadata is a `TokenLimits` pair or absent; see the
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
- [Migration inventory](migration_inventory.md): files requiring disposition decisions;
  most fates remain open, including the cluster guide's.
- Supporting research: [LiteLLM metadata](litellm_metadata.md),
  [Antigravity limits](antigravity_limits.md), and [client budgets](client_budgets.md).
- [Tracking issue #9121](https://github.com/agentydragon/ducktape/issues/9121): PR/rollout
  status and parked integrations, including restoration obligations.
