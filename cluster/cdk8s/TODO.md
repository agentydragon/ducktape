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
hand-written, so unifying them needs a YAML→cdk8s conversion (or, for two entries
below, a non-Kubernetes fix in Terraform) before the drift can be closed by
construction. Candidates where both sides were already cdk8s-generated Python were
fixed directly instead of listed here (see git log — `cluster/cdk8s/litellm_config.py`,
`model_rosters.py`, `agentplane/staging.py`, `generate_manifests.py`,
`app_constructs.py`, `egress_constructs.py`, `dex_constructs.py`,
`ha_mcp_constructs.py`, and the validation tests that pinned the now-redundant
equalities).

Entries are removed once landed — this is a burn-down, not a changelog.

## Flagship: `nebula-mesh.json` should be the only place the node roster lives

`nebula-mesh.json`'s own `_comment` field already calls itself "Single source of
truth for the Nebula mesh host roster" and lists its consumers — but two real
consumers aren't on that list and don't actually read the file:

- `cluster/terraform/main/ovh-nodes.tf` hand-types the control-plane node
  IPs/hostnames as separate HCL `locals` (`test_nebula_mesh.py` pins these against
  the JSON's own `Mesh` roster).
- `cluster/k8s/monitoring/etcd/endpoints.yaml` hand-types the same IPs a third time
  as a hand-written Kubernetes `Endpoints`/`EndpointSlice` manifest.
- `tf/gitops/dns-records/main.tf`'s `local.public_gateway_ips`/`kube_api_ips` are a
  _fourth_ independent hand-typed copy (`test_dns_records.py`).

Terraform can `jsondecode(file("${path.module}/../../../nebula-mesh.json"))`
directly — this doesn't need a cdk8s conversion, just wiring the existing JSON into
the `.tf` locals instead of retyping them. `monitoring/etcd/endpoints.yaml` is a
plain Kubernetes manifest with no cdk8s presence at all yet; either convert it to a
small cdk8s chart reading the same roster (`cluster.scripts.nebula_mesh` already
parses it in Python — see `test_roaming_daemonset_capacity.py`'s use of it), or
generate it via the same `jsondecode` approach if a non-cdk8s generator is
preferred. Once this lands, `test_nebula_mesh.py`'s cross-source IP-agreement test
and `test_dns_records.py`'s equivalent collapse to unreachable-by-construction.

## Flagship: one central LLM model registry

`cluster/cdk8s/model_rosters.py` is already the shared roster (`ANTHROPIC_MODELS`,
`GEMINI_MODELS`, `CLIPROXY_MODELS`, `TANA_MODELS`, `OPENCLAW_CLIPROXY_MODEL_LIMITS`,
`Provider`/`ApiShape`/`exposed_name()`/`shape_for()`) and most of `litellm_config.py`,
`public_coder_agent_config.py`, `staging_config.py`/`testing_config.py`, and
`haku_openclaw_spike_config.py` already pull from it — that's why the audit found
very few _already-cdk8s_ duplicates once the tana/qwen3-embedding/web-push fixes
landed. But the roster is still parallel arrays (a list of ids here, a dict of
context windows there, a dict of display names in a third file) rather than one
registry a model is a member of. Concretely still open:

- **Model slug construction is duplicated at the call site, not the data.**
  `litellm_config.py` and `public_coder_agent_config.py` each independently call
  `exposed_name(Provider.CHATGPT, ApiShape.OAI_RESPONSES, model)` to name "this Codex
  model as served through LiteLLM" — same provider+shape pair, hand-typed at two call
  sites instead of coming from one named helper (e.g. `codex_litellm_id(model)` in
  `model_rosters.py`). If Codex's shape or provider prefix ever changes, only one
  call site is guaranteed to notice.
- **Display names live outside the roster.** `public_coder_agent_config.py`'s
  `_CODEX_DISPLAY_NAMES`/`_GEMINI_DISPLAY_NAMES` dicts are keyed by the same model
  ids `model_rosters.py` already lists, but aren't attached to them — nothing stops
  the two lists from silently diverging (a model added to `GEMINI_MODELS` without a
  display name fails only when someone notices the catalog entry looks wrong).
- **Terraform can't consume the roster at all.** `tf/gitops/litellm-keys/main.tf`
  hand-types five separate model-name allowlists (`oai_lane_models`,
  `codex_client_models`, `claude_client_models`, and two more) that
  `test_litellm_config.py`/`test_openclaw_models.py`/`test_model_roster_consumers.py`
  (5 test functions total) exist purely to keep in sync with `model_rosters.py`.
  Same `jsondecode` fix as the nebula-mesh entry: export the relevant roster lists as
  a small generated JSON file Terraform reads, instead of retyping model names in
  HCL.

The shape to grow toward (not a full redesign — extend what's there): a small
frozen dataclass per model — id, display name, context window, max tokens,
provider, shape — with `model_rosters.py` holding one registry of these instead of
parallel `_MODELS` lists plus separate `_LIMITS`/`_DISPLAY_NAMES` dicts keyed the
same way in three different files. `litellm_config.py`, the OpenClaw configs, and
(via the JSON export above) Terraform would all read attributes off the same
objects instead of reconstructing them. Worth a short design pass before touching
this broadly — it's the one item here big enough to warrant a plan, not a
find-and-replace.

## Follow-ups from the agentplane conversion

- **A light `settings.py` per agentplane service.** Synth imports each service's
  `main` for its `Settings`, pulling mitmproxy/fastapi in; the synth tests sit at
  `size = "medium"` for that alone. Moving `Settings` and the sub-models it needs
  into a `settings.py` the constructs import returns them to `small`.
- **Fleet rules on the litellm and ha-mcp charts.** `fleet_rules.add_fleet_rules`
  runs on the agentplane chart only; the other two need `provided_secrets` rosters.
- **`Chart(namespace=...)`** once cluster-scoped objects (ClusterRole/Binding, the
  trust-manager Bundle) move to their own chart; then `metadata(name, namespace)`
  drops out of every namespaced object.
- **One PodDisruptionBudget helper** for the three `_add_pdb` copies (actions, app,
  egress).
- **Cilium peers as constructs.** `cilium_helpers.endpoint_labels(namespace, name)`
  takes strings; the target construct's exported labels would make a renamed workload
  fail at synth instead of at runtime.

## Ready to convert — small, focused, and the pattern to copy already exists in this repo

- **`cluster/k8s/agents/haku-egress-proxy/` and `cluster/k8s/agents/mitmproxy/`
  CiliumNetworkPolicy `toFQDNs`/`server_names`** (`test_egress_allowlists.py`,
  `test_dns_rule_matches_the_allowlist`). Same shape as the web-push fix just
  applied in `agentplane/staging.py` — a `toFQDNs` list and a
  `toPorts.rules.dns`/`server_names` list built from two separate hand-written YAML
  blocks instead of one Python tuple. Convert these two CiliumNetworkPolicies to
  cdk8s and build both lists from one tuple the same way.
- **ClickHouse schema Job / aiquota migrate container health-check literal**
  (`test_clickhouse_distributed_ddl_contract.py`). The Job's own
  apiVersion/kind/name/namespace is hand-retyped into the Flux Kustomization's
  `healthChecks` entry. `generate_manifests.py` already has the fix pattern for
  this exact shape (`KustomizationSpecHealthChecks` built from the same
  `name`/`namespace` the chart itself uses, e.g. `_generate_ha_mcp`) — needs
  `cluster/k8s/clickhouse/schema/` and `cluster/k8s/aiquota/` converted first.
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

- **`cluster/k8s/haku/console/`** — `test_haku_deployment_config_contract.py` and
  `test_haku_deployment_contract.py` cross-check Service selectors against
  Deployment labels, static Service `targetPort` against container ports, HTTPRoute
  backends against Service names, `dependsOn` sets across 3 `flux-kustomization.yaml`
  files, and the console's ssh-mcp wiring — all hand-typed relationships within and
  around one directory that has zero cdk8s footprint today.
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
