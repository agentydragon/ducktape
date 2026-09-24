# cluster/cdk8s TODO

Tests under `cluster/validation/` and elsewhere exist, in large part, because two
pieces of hand-written YAML (or a manifest and application code) have to agree on some
value and nothing computes one side from the other. The fix in each case below isn't
"write a test" — it's plumb the value through one Python source so the two sides
can't drift _by construction_; a future edit that reintroduces two independent
literals is then a visible, reviewable diff instead of a silent runtime break.

This file tracks the candidates found by a full-repo audit (every test that reads a
cdk8s-generated YAML file, plus every `cluster/validation/` test) that are not closed by
construction yet. Candidates where both sides were already cdk8s-generated Python were
fixed directly instead of listed here (see git log — `cluster/cdk8s/litellm/config.py`,
`model_rosters.py`, `agentplane/staging.py`, `generate_manifests.py`,
`app.py`, `egress.py`, `dex.py`,
`ha_mcp.py`, and the validation tests that pinned the now-redundant
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
- **Cilium peers as constructs.** `cilium.endpoint_labels(namespace, name)`
  takes strings; the target construct's exported labels would make a renamed workload
  fail at synth instead of at runtime.
- **The egress fence as one construct.** A Pod sits behind the central proxy only with
  all five of: the sidecar container, the projected-token volume, the CA mount, the
  routing env on its workload container, and the labels `networkpolicy-runner` selects.
  Those live inline across `_runner_container` and `_add_sandbox_template`, which is fine
  for one template and a trap for the second — the sandbox Actions' `exec` reached
  nothing because the routing env was a runner argument rather than container env, and
  the fence correctly denied everything else. When an exec-target template earns its own
  image, have one construct take a workload container and return the fenced pod spec,
  so the five hold together by construction instead of by being copied. Not worth
  extracting while `agentplane-runner` is the only caller.

## After cdk8s-based manifest builds stabilize

- **Consider restoring cross-chart Pod Secret/ConfigMap dependency validation.** Once the
  new cdk8s-based cluster manifest builds are stable, revive it if a build or generation
  component can see the needed graphs together: chart objects and controller-created
  outputs, sibling Kustomize resources, and the Flux `dependsOn` graph. A per-chart
  synth cannot establish relationships across those boundaries. The removed checks were:
  - Require each Pod Secret/ConfigMap reference not produced in its chart to name a
    declared external provider.
  - Require that provider to appear in the consumer's Flux dependency or sibling-resource
    inventory.
  - Reject declared providers no Pod reads. These were source-graph consistency checks;
    they did not query the cluster or prove a live resource existed.

- **Check ESO wiring against each store.** An `ExternalSecret`, or a
  `ClusterExternalSecret.spec.namespaces` entry, only syncs if the backing
  `ClusterSecretStore.spec.conditions[].namespaces` admits its namespace, and, for a store
  with referent auth (`auth.serviceAccount` without a namespace), only if that namespace
  has the named ServiceAccount. Each is a pair of independently written lists that drift
  silently: ESO reports `SecretSyncedError` on the live cluster and nothing in CI fails.
  It has shipped three times: PR #7407 widened `google-access-token`'s
  `ClusterExternalSecret` to add `agentplane-staging` without widening
  `kubernetes-airlock-secret-store`'s `conditions`; `kubernetes-forgejo-images-secret-store`
  never admitted `google-mcp`'s pull secret (#7630); and agentplane-testing's GitHub PAT
  copy lost its `external-creds-reader` ServiceAccount. `cluster/validation/checks.py`
  already sees rendered and hand-written resources together (the store checks beside
  `check_forgejo_image_namespace_reflection`), so resolve every (Cluster)ExternalSecret's
  store there and fail on either gap. Once every pair is a cdk8s construct, derive both
  sides from one source instead.

  Specific coverage removed:
  - Agentplane staging: `agentplane-oidc` and `agentplane-mcp-oauth` from
    `sso-providers-tf`, `litellm-key-agentplane-staging` from `litellm-keys-tf`,
    `ssh-mcp-bearer` from `ssh-mcp`, `haku-console-github-mcp-client-credentials` from
    `haku-console`, and `agentplane-staging-web-push-vapid` from its SOPS sibling.
    Testing checked `litellm-key-cheap-experiments` from `litellm-credentials/`.
  - aiquota: `aiquota-api-bearer` from SOPS, `cli-proxy-api-management` from
    `cli-proxy-api`, `aiquota-oidc` from `agent-machine-access-tf`, and
    `clickhouse-aiquota-credentials` from `reflector`; ConfigMaps generated from
    `config.toml` and `schema.sql`.
  - ClickHouse schema: `clickhouse-admin-credentials` from `clickhouse` and the
    `schema.sql` ConfigMap.
  - Haku console: Secrets `forgejo-images-creds`, `haku-console-oidc`,
    `haku-console-public-coder-agent`, `haku-console-agent-api`, `ssh-mcp-bearer`,
    `aiquota-api-bearer-haku-console`, `haku-routine-launch-token`,
    `haku-console-web-push-vapid`, and `haku-console-github-mcp-client-credentials`;
    ConfigMaps from `static-metadata.yaml`, `image-metadata.yaml`, and `indexer-role.sql`.
  - ha-mcp: `home-assistant-break-glass`, the SOPS `ha-mcp-bearer`, and the
    Job-created `ha-mcp-home-assistant-token`.
  - ssh-mcp: `ssh-mcp-keys`, `ssh-mcp-keys-public-coder-devbox`, and `ssh-mcp-keys-atlas`.
  - LiteLLM: `litellm-master-key`, `litellm-salt-key`, `litellm-anthropic-key`,
    `litellm-groq-key`, `litellm-gemini-key`, `litellm-mistral-key`,
    `litellm-cliproxy-key`, `litellm-db-app`, `langfuse-secrets`, and
    `tana-firebase-refresh-token`.
  - `ntfy`, `etcd-monitoring`, and Forgejo image automation had empty provided-resource
    rosters, so any new out-of-chart Pod reference failed validation.
  - Unit cases covered valid `oidc` → `sso-tf` and `image-tag` → `image-tag.yaml`
    declarations, unprovided `nobody-makes-this` Secret and `nor-this` ConfigMap,
    `token` mapped to absent provider `token-maker`, and unused `stale` Secret and
    `stale-map` ConfigMap entries.

## Ready to convert — small, focused, and the pattern to copy already exists in this repo

- **`openclaw-spike-iron.yaml`'s `allowlist` transform** (`test_egress_allowlists.py`,
  `test_openclaw_spike_resolves_exactly_its_iron_allowlist`). The spike's Cilium DNS
  rule is generated from `egress_fences.OPENCLAW_SPIKE_ALLOWLIST`, but the iron config
  it mirrors is still a hand-written `configMapGenerator` input. Render the iron
  ConfigMap from the same tuple (the `<name>-config.k8s.yaml` shape
  `agents/public-coder-agent/app` uses) and the pin collapses.

## Reachable since the one-to-one conversion — the manifests are generated, the tests remain

Each side the test compares is now a construct, except the hand-written inputs named;
retiring a test means deriving both sides from one value.

- **`agents/public-coder-agent/{app,proxy,devbox}`, `agent-rbac-base` and
  `clickhouse/cluster`** — `test_haku_public_coder_contract.py` and
  `test_public_coder_clickhouse_reader_contract.py` check subject, selector and port
  agreement across RBAC, NetworkPolicies and the proxy. Still hand-written: the Iron
  transform configs (`proxy/iron.yaml`) and the app's `agent-kubeconfig.yaml`, both
  `configMapGenerator` inputs.
- **`agents/haku-egress-proxy` script contract** — `test_haku_sandbox_contract.py`
  regex-extracts required env vars and a clone host:port from `haku-sandbox-setup.sh`
  (an image build input) and checks the generated SandboxTemplate
  (`haku/workspaces.py`) and egress policy cover them. Closing it fully needs the script
  to declare its requirements in a checkable form.
- **`authentik/app`** — `test_authentik_blueprint_contracts.py`'s
  `configMapGenerator.files` list vs. a glob of `blueprints/*.yaml`: the
  `kustomization.yaml` is still hand-written, so a generated one listing the glob would
  close it. The outpost/provider referential-integrity half walks Authentik's blueprint
  DSL (`!Find`/`!KeyOf`) and stays an external-format check.
- **`haku/mailbox`** — `test_mailbox_plan.py`'s init/prod image equality and
  configMapGenerator-name-vs-mount-name checks, now against `haku/mailbox.py`.

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
`agentplane/acceptance/test_egress.py`, `cluster/cdk8s/crd_bindings/flux/test_kustomization_import.py`);
and pure unit/schema tests with no
second independently-authored source to drift against (`test_checks.py`,
`test_cluster.py`, `test_crd_layering.py`, `test_dependencies.py`, `test_flux.py`,
`test_generator_namespace.py`, `test_health_checks.py`, `test_image_automation.py`,
`test_k8s.py`, `test_postbuild_substitutions.py`, `test_sops_decryption.py`).
