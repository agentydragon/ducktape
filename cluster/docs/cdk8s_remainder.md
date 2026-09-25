# Remaining YAML and configuration boundaries

Source audit at `eec338cb4e`, 2026-09-24. Priorities live in
[the adoption plan](../cdk8s/PLAN.md). Treatments below are recommendations unless
identified as an existing boundary; they do not authorize a runtime ownership change.

The conversion of ordinary active resources under `cluster/k8s` does not cover every
deployment package, Kustomize overlay, embedded config string or application language.
Inventory by writer and consumer, not extension. To refresh the hand-written candidates
(including generated-by-other-tools files):

```bash
git ls-files 'cluster/k8s/**' |
  git check-attr --stdin linguist-generated |
  awk -F ': ' '$3 != "true" {print $1}'
```

Exclude parked packages only when prioritizing active work, not when describing total
adoption. `*.k8s.yaml` is generated output; its presence does not make neighboring
configuration typed.

## Convert: configuration with an existing application contract

### Grocy household overlays and policy

`cluster/k8s/grocy/{sf,vallejo}/mcp/{config,kustomization}.yaml` configures two
instances and patches household-specific OIDC Secret references into the generated
`mcp-base` Deployment. `cluster/cdk8s/grocy/mcp.py` already has household builders;
`grocy_mcp/mcp_types.py:ServerSettings` owns the runtime settings contract.

Proposed: pass household values into direct per-household resource construction and
render non-secret settings from that contract. Remove the generated-base/YAML-patch
round trip. Keep the shared bot-owned image pin until image ownership changes explicitly.
Use the same treatment for the app/user-perms overlays where they express household
variation.

`grocy/user-perms-base/policy.yaml` uses a YAML anchor for the human users' permission
set and an explicit empty set for Haku. Its consumer already has
`cluster/provisioners/grocy_user_perms/provision.py:Policy`. A typed generated policy
can share the human permission set while preserving Haku's empty set. Keep live
permission-name validation: the installed Grocy API owns that vocabulary.

Done: both households render from parameters without Secret-reference patches; policy
meaning is unchanged after anchor expansion, serialization is deterministic, and
ConfigMap name rewriting/rollout behavior is preserved.

### Airlock

`cluster/k8s/agents/airlock/config.yaml` is non-secret broker configuration.
`airlock/config.py:Settings` and `airlock/oauth/provider.py:OAuthConfig` already
model it. The latter currently lives beside runtime HTTP code.

Proposed: extract the lightweight contract, then render the config and related
Secret names, provider env bindings and distribution references from shared values.
Use the existing settings-contract mechanism for runtime-supplied secrets; do not
instantiate Settings using ambient credentials during synthesis.

Keep `google` and `google-write` as separate grants and consumers. Preserve the
explicit Oura/BSC callback URIs until their external client registrations change.
The generator can define desired broker configuration; token exchange/refresh and
writing runtime token Secrets remain the broker's responsibility.

Done: no independently written provider/Secret roster across config and deployment,
no secret material in generated output, unchanged OAuth scopes and registered callbacks.

### Authentik, Forgejo and Attic rotator rosters

Inputs: `agents/authentik-jwt-rotation/rotations.yaml`,
`agents/forgejo-token-rotation/tokens.yaml`, `nix-cache/rotators.yaml`.
Each consumer under `cluster/rotators/{authentik_jwt_rotation,forgejo_token_rotation,
attic_jwt_rotation}/rotate.py` already defines its own Config and entry model.

Proposed: move these schemas out of runtime modules and build each roster as typed
application configuration. Derive credential mounts and output Secret names from those
entries wherever the generator currently repeats them. Keep audiences, scopes and
consumer grants explicit. These are three reviewable changes, not a generic rotator
framework.

The rotators write SOPS ciphertext and sometimes publish more than one output. cdk8s
must not become a second writer of those bytes. Consolidating those outputs, or
replacing token-minting API calls with a provider, changes lifecycle ownership and needs
its own investigation.

Done: config and mounts share inputs, runtime rotation semantics and output owners are
unchanged, and generated ConfigMaps contain no credentials.

## Model selectively: third-party configuration

### Gatus

`cluster/k8s/gatus/config.yaml` combines endpoint identity with monitoring intent:
health URLs, expected statuses, body predicates and an inference request.
Proposed: derive owned hostnames, service addresses and model names from exported
values while retaining explicit probe selection and assertions. Do not automatically
turn every HTTPRoute into a health check or infer that a 200 response is sufficient.

Preserve application-expanded `${GATUS_CLIENT_SECRET}`, `${GATUS_DB_URI}` and
`${LITELLM_API_KEY}` literally. Validate with the pinned consumer/schema; do not use
whole-resource Flux substitution to inject these runtime values.

Done: shared identities come from their owners, and probes retain their authentication
and behavioral meaning.

### Authentik blueprints: ownership first

`cluster/k8s/authentik/app/blueprints/*.yaml` uses `!Find`, `!KeyOf`, object
identifiers, and `state: absent` cleanup entries. `embedded-outpost.yaml` repeats
proxy-provider membership. These are Authentik's declarative language, not CRDs;
`cdk8s import` cannot model them as Kubernetes objects.

Follow [the SSO ownership direction](sso.md): Terraform is preferred for provider
objects; blueprint-managed providers remain a migration backlog. Inventory ownership
per object before transferring a provider, application, policy binding or outpost.
Do not generate a new competing SSO catalog. Remaining bootstrap/flow blueprints may
stay native YAML; a small tag-aware renderer is worth considering only for repeated
blueprint structures that retain blueprint ownership.

The [upstream blueprint contract](https://docs.goauthentik.io/customize/blueprints/v1/structure/)
distinguishes omitted attributes, create-only state and absent state. A plain
JSON/Pydantic dump is not equivalent to tagged YAML. Removing a file is not deletion of
the corresponding Authentik object. Never carry a tombstone into an object now owned
by Terraform.

Done per migration: one writer per Authentik object, intact access policy and outpost
routing, tags/omissions preserved where blueprints remain, and lifecycle behavior
verified against Authentik.

### Other payloads and Helm values

Keep SQL, Nginx/Caddy/CoreDNS configuration, Alloy, dashboard JSON, shell scripts and
static known-hosts files in their native form unless shared values or repeated
structures justify generation. Examples remain under `activitywatch`, `monitoring`,
`haku/mailbox`, `nix-cache`, `oci-cache`, `ollama` and `seaweedfs/cluster`.
`home-assistant/app/configuration.yaml.conf` is YAML despite its suffix.

Similarly, `headlamp.py:_PLUGINS_CONFIG` is still YAML embedded in a Python string,
and Helm `values` dictionaries are only partially typed. Use existing consumer
models or pinned chart schemas/templates where useful. Typing the HelmRelease envelope
does not validate the chart's arbitrary values. Do not hand-maintain full vendor schemas
just to eliminate dictionaries.

Generated Kustomize wrappers can package these files without converting their contents.
Extend the small Kustomize model for an actually used field (`patches`,
`configurations`, generator options) only where it removes a concrete authoring seam.

## Kubernetes manifests and deliberate external owners

- **Remote installations:** `agents/agent-sandbox/controller/{kustomization,patches}.yaml`
  and `kubevirt/{operator,cdi-operator}/{kustomization,namespace-patch}.yaml` compose
  upstream release bundles with local patches. Keep upstream release ownership.
  Generate local typed patches/wrappers where worthwhile; check against the actual
  pinned release render. Do not transcribe upstream controllers/CRDs into local Python.
- **Other generators:** `flux/flux-system` belongs to Flux bootstrap;
  `agentplane-crds/crd-*.yaml` belongs to `//agentplane/crds:generate_bin`. Their
  YAML is not missing hand-written-to-cdk8s work.
- **SOPS and image automation:** ciphertext stays with SOPS/key holders or its rotator;
  `image-pins` and Haku's `{image,static}-metadata.yaml` stay bot-owned. cdk8s owns
  references/composition. Zero hand-written YAML is not an appropriate target for them.
- **Project-owned deployments:** `props/deploy` and `loom/wayback/deploy` retain raw
  resources and suspended, unwired Flux declarations. Haku dispatch and managed-agent
  packages have suspended nodes in the generated chart. Keep them in the adoption
  inventory but convert as part of deliberate revival; do not reactivate to measure
  coverage. Other raw packages (`devinfra/firecracker/deploy`,
  `haku/x/zones/deploy`, `x/codex_pod_image/deploy`) need an owner/use decision;
  no active central node for them was found in this audit.
- **Parked/vendor/example trees:** `cluster/k8s/parked`, vendored Browsertrix charts,
  archived experiments and documentation examples are outside the active conversion
  target. Their presence must not inflate an active-manifest completion percentage.

## Mixed-directory layout: preserved mechanism findings

These are findings recorded by the prior plan's 2026-09-24 investigation, not experiments
rerun by this source review. Pins then: kustomize-controller v1.9.5,
`fluxcd/pkg/kustomize` v1.35.6, source-watcher v2.2.4,
image-automation-controller v1.2.5, kustomize v5.5.0. Recheck on implementation.

- Cross-root resource/Component references built when both paths were in the artifact.
  Flux uses `LoadRestrictionsNone` inside the extracted artifact boundary; a local
  cross-root file needs that flag too, while a Component directory built with RootOnly.
  A cpap-sync split preserved its eight rendered objects in the recorded experiment.
- Artifacts preserve repo paths. The current helper's `directory/**` copy is suitable.
  A general `*.sops.yaml` glob retains its full path in source-watcher, while
  `render_diff.py:apply_copy` strips the non-glob prefix for all globs. Repair/test that
  mismatch before relying on such globs.
- Copies are ordered; a later one can overwrite an earlier file. A missing required
  file/glob can fail the entire ArtifactGenerator reconciliation, affecting unrelated
  artifacts. Validate inputs before introducing cross-root composition.
- Decryption applies to built resources, so cross-root SOPS resources still need the
  consumer's decryption configuration.
- Flux image automation rewrites marked YAML under `update.path`, including kind-less
  YAML and ConfigMap data. Its writable source and update path currently cover
  `cluster/k8s`. A generated file cannot share byte ownership with the bot.
- Flux postBuild substitution reaches ConfigMap strings and decrypted Secret values.
  The recorded non-strict experiment turned an undefined `${HOME}` into empty text
  and `p${ass}word` into `pword`. App-owned interpolation makes this an unsuitable
  default image-pin replacement.
- Flux `spec.components` can reference a copied cross-root Component even when Flux
  creates the directory's Kustomization. Kustomize replacements from a local-config
  ConfigMap can target image fields and env metadata without an applied ConfigMap;
  the recorded experiment verified the local-config object was omitted from output.

Recommendation: keep current colocation while converting useful seams. If tree purity
later has a concrete benefit, trial one per-app hand-written Component holding SOPS and
image pins, copied into the artifact and referenced by the generated directory. This
would change the current whole-directory rule and remains an open design decision.

Alternatives already considered: separate Secret Flux nodes add owners/readiness edges;
ciphertext copying adds regeneration on rotation; exempting SOPS makes the generated
tree mixed; composition only in artifacts makes checkout rendering differ from deployment.
Moving secrets out of Git is a separate lifecycle migration. Reading bot tags into
synthesis adds regeneration to every image bump; postBuild adds interpolation hazards;
bot-writing generated files breaks writer ownership; Bazel image digests would replace
the explicitly retained Flux image bumper. A write-once pin-file generator introduces
another ownership convention. Kustomize replacements are worth a targeted trial for
nonstandard image/env paths, not a fleet-wide rewrite.

Repair gates before using new mechanisms. Never change Flux owners or add dozens of
Secret Kustomizations solely to move files between roots.
