# cluster/cdk8s TODO

Tests under `cluster/validation/` and elsewhere exist, in large part, because two
pieces of hand-written YAML (or a manifest and application code) have to agree on some
value and nothing computes one side from the other. The fix in each case below isn't
"write a test" — it's plumb the value through one Python source so the two sides
can't drift _by construction_; a future edit that reintroduces two independent
literals is then a visible, reviewable diff instead of a silent runtime break.

This file tracks the candidates found by a full-repo audit (every test that reads a
cdk8s-generated YAML file, plus every `cluster/validation/` test) that are **not yet
reachable from cdk8s** — the manifests/configs on one or both sides are still
hand-written, so unifying them needs a YAML→cdk8s conversion before the drift can be
closed by construction. Candidates where both sides were already cdk8s-generated Python were
fixed directly instead of listed here (see git log — `cluster/cdk8s/litellm_config.py`,
`model_rosters.py`, `agentplane/staging.py`, `generate_manifests.py`,
`app_constructs.py`, `egress_constructs.py`, `dex_constructs.py`,
`ha_mcp_constructs.py`, and the validation tests that pinned the now-redundant
equalities).

Entries are removed once landed — this is a burn-down, not a changelog.

## Follow-ups from the agentplane conversion

- **A light `settings.py` per agentplane service.** Synth imports each service's
  `main` for its `Settings`, pulling mitmproxy/fastapi in; the synth tests sit at
  `size = "medium"` for that alone. Moving `Settings` and the sub-models it needs
  into a `settings.py` the constructs import returns them to `small`.
- **`Chart(namespace=...)`** once cluster-scoped objects (ClusterRole/Binding, the
  trust-manager Bundle) move to their own chart; then `metadata(name, namespace)`
  drops out of every namespaced object.
- **One PodDisruptionBudget helper** for the three `_add_pdb` copies (actions, app,
  egress).
- **Cilium peers as constructs.** `cilium.endpoint_labels(namespace, name)`
  takes strings; the target construct's exported labels would make a renamed workload
  fail at synth instead of at runtime.

## Ready to convert — small, focused, and the pattern to copy already exists in this repo

- **`openclaw-spike-iron.yaml`'s `allowlist` transform** (`test_egress_allowlists.py`,
  `test_openclaw_spike_resolves_exactly_its_iron_allowlist`). The spike's Cilium DNS
  rule is generated from `egress_fences.OPENCLAW_SPIKE_ALLOWLIST`, but the iron config
  it mirrors is still a hand-written `configMapGenerator` input. Render the iron
  ConfigMap from the same tuple (the `<name>-config.k8s.yaml` shape
  `agents/public-coder-agent/app` uses) and the pin collapses.
- **`ssh-mcp` known_hosts / sshpiper pinning** (`test_ssh_mcp_known_hosts.py`,
  and `cluster/validation/test_ssh_mcp_consumers.py`).
  `cluster/k8s/ssh-mcp/known_hosts`, `ssh_keys/public-coder-devbox-host.pub`,
  `cluster/k8s/ssh-mcp/settings.yaml`, and
  `cluster/k8s/agents/public-coder-agent/sshpiper/pipe-devbox.yaml` all hand-copy
  the same host key/hostname. `ssh_keys/public-coder-devbox-host.pub` is the natural
  canonical source; a small generator reading it and writing both `known_hosts` and
  sshpiper's base64 blob is the same "mostly hand-written directory, one generated
  file" shape `ha_mcp_constructs.py` already uses for `bearer.sops.yaml`'s sibling
  files — doesn't need the whole `ssh-mcp/` directory converted.

## Larger conversions — whole hand-written directories, no cdk8s presence yet

- **`cluster/k8s/agents/public-coder-agent/{app,proxy,devbox}/`,
  `agent-rbac-base/`, and `clickhouse/cluster/`** — `test_haku_public_coder_contract.py`
  and `test_public_coder_clickhouse_reader_contract.py` tie together ~15-22
  hand-written manifests (RBAC roles/bindings, NetworkPolicies, a kubeconfig, a SOPS
  secret, Iron proxy transform config) on subject/selector/port agreement. Only
  `public-coder-agent/app`'s one ConfigMap is cdk8s-generated today; `deployment.yaml`,
  `role.yaml`, the NetworkPolicies, and the proxy/devbox directories are not.
- **`cluster/k8s/agents/haku-egress-proxy/` script contract** —
  `test_haku_sandbox_contract.py` regex-extracts required env vars and a clone
  host:port from `haku-sandbox-setup.sh` and checks a hand-written SandboxTemplate
  and egress policy cover them. Converting the SandboxTemplate/policy alone doesn't
  fully close this (the script itself would need to declare its own requirements in
  a checkable form), but it removes half the duplication.
- **`cluster/k8s/authentik/app/`** — `test_authentik_blueprint_contracts.py`'s
  `configMapGenerator.files` list vs. a glob of `blueprints/*.yaml` is a plain
  hand-typed-list-vs-directory-contents check a cdk8s `glob()` at generation time
  would close. The outpost/provider referential-integrity half of that test walks
  Authentik's own blueprint DSL (`!Find`/`!KeyOf`) and stays a real external-format
  check regardless of conversion.
- **`cluster/k8s/haku/mailbox/app/`** — `test_mailbox_plan.py`'s init/prod image
  equality and configMapGenerator-name-vs-mount-name checks. Small enough this might
  not be worth a dedicated cdk8s chart on its own; reconsider if `mailbox/` gets
  touched for another reason first.
- **`generators.yaml` / `test_actions_artifact.py`** — the hand-written
  `artifact-generators/generators.yaml` retypes, per artifact, the source directory
  that the directory's own `flux-kustomization.yaml` already names in `spec.path` and
  `sourceRef` (~200 artifacts). The test discovers those consumers and checks the two
  agree; one generator emitting both sides would make that hold by construction.
  Largest single item here by far; needs its own scoping pass rather than folding
  into an existing directory's conversion.

## Parked — lower priority

- **`haku/x/dispatch/` (`test_haku_dispatch_zones_contract.py`)** — `zones.yaml`'s
  `models:` list duplicates `generate_workers_litellm.py`'s
  `zai_zone_model_names()` (itself derived from `ZAI_ANTHROPIC_MODELS`). The fix
  shape is clear (extend that generator to also write `zones.yaml`, same as it
  already writes the workers-litellm config) but `haku/x/dispatch/` is parked
  infrastructure — converting it means building new cdk8s for something not
  currently running. Revisit if/when dispatch is unparked.

## Not applicable — checked and left alone

Confirmed via the same audit and intentionally _not_ listed above:
GENUINE-BOUNDARY tests that exercise a real external tool or a different deployable
(all of `cluster/validation/kyverno/`, `test_flux_build.py`, `test_helm_templates.py`,
`test_github_proxy_rules.py`, `test_github_quota_rules.py`,
`test_cluster_integration.py`'s `kustomize build` core,
`x/agentplane/acceptance/test_egress.py`, `third_party/flux/test_kustomization_import.py`);
and pure unit/schema tests with no
second independently-authored source to drift against (`test_checks.py`,
`test_cluster.py`, `test_crd_layering.py`, `test_dependencies.py`, `test_flux.py`,
`test_generator_namespace.py`, `test_health_checks.py`, `test_image_automation.py`,
`test_k8s.py`, `test_postbuild_substitutions.py`, `test_sops_decryption.py`,
`test_terraform_backends.py`).
