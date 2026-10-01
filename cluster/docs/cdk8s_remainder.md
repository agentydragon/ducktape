# Remaining YAML and configuration boundaries

Source audit at `f84301e72c`, 2026-09-28. Priorities live in
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

## Model selectively: third-party configuration

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

Keep SQL, Nginx/Caddy configuration, Alloy, dashboard JSON, shell scripts and
static known-hosts files in their native form unless shared values or repeated
structures justify generation. Examples remain under `activitywatch`, `monitoring`,
`haku/mailbox`, `nix-cache`, `oci-cache` and `seaweedfs/cluster`.
`home-assistant/app/configuration.yaml.conf` is YAML despite its suffix.

Helm `values` dictionaries are only partially typed. Use existing consumer models or
pinned chart schemas/templates where useful. Typing the HelmRelease envelope does not
validate the chart's arbitrary values. Do not hand-maintain full vendor schemas just to
eliminate dictionaries.

Generated Kustomize wrappers can package these files without converting their contents.
Extend the small Kustomize model for an actually used field only where it removes a
concrete authoring seam.

## Kubernetes manifests and deliberate external owners

- **Other generators:** `flux/flux-system` belongs to Flux bootstrap. Its YAML is not
  missing hand-written-to-cdk8s work.
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
- **Parked/vendor/example trees:** `cluster/parked`, vendored Browsertrix charts,
  archived experiments and documentation examples are outside the active conversion
  target. Their presence must not inflate an active-manifest completion percentage.

## Mixed-directory layout

**Rule: image pins cross the roots; nothing else does.** A directory whose only
hand-written file is an `image-pins` Component lives under `cluster/generated`. The
Component stays under `cluster/k8s`, inside image automation's writable checkout and
`update.path`, as a directory of its own (`cluster/k8s/grocy/mcp-image-pins`) at no
sub-path a generated directory occupies, so no directory is split across the roots. The
generated `kustomization.yaml` names it as a relative `components:` entry, and the
directory's artifact copies it at its repo path after the generated directories.
`cluster/cdk8s/grocy` is the instance. A directory holding SOPS ciphertext or another
hand-written file keeps colocation under `cluster/k8s`; moving SOPS Secrets across the
roots is an open design decision.

Mechanism findings from the 2026-09-24 investigation, at kustomize-controller v1.9.5,
`fluxcd/pkg/kustomize` v1.35.6, source-watcher v2.2.4, image-automation-controller
v1.2.5 and kustomize v5.5.0. Those marked _rechecked_ were re-verified on the grocy
directories by the check named; recheck the others before relying on them.

- Cross-root resource/Component references build when both paths are in the artifact.
  Flux uses `LoadRestrictionsNone` inside the extracted artifact boundary; a local
  cross-root file needs that flag too, while a Component directory builds with the
  default `LoadRestrictionsRootOnly` (_rechecked_: `kustomize build` of the grocy
  directories in `//cluster/validation:test_cluster_integration`).
- Artifacts preserve repo paths, so a relative cross-root reference resolves the same in
  the checkout and in the artifact (_rechecked_: `render_diff.py` renders the moved grocy
  artifacts to identical objects). The helper's `directory/**` copy is suitable. A
  general `*.sops.yaml` glob retains its full path in source-watcher, while
  `render_diff.py:apply_copy` strips the non-glob prefix for all globs. Repair/test that
  mismatch before relying on such globs.
- Copies are ordered; a later one can overwrite an earlier file. A missing required
  file/glob can fail the entire ArtifactGenerator reconciliation, affecting unrelated
  artifacts. Validate inputs before introducing cross-root composition.
- Decryption applies to built resources, so cross-root SOPS resources still need the
  consumer's decryption configuration.
- Flux image automation rewrites marked YAML under `update.path`, including kind-less
  YAML and ConfigMap data. Its writable source and update path cover `cluster/k8s`. A
  generated file cannot share byte ownership with the bot.
- Flux postBuild substitution reaches ConfigMap strings and decrypted Secret values.
  The recorded non-strict experiment turned an undefined `${HOME}` into empty text
  and `p${ass}word` into `pword`. App-owned interpolation makes this an unsuitable
  default image-pin replacement.
- Flux `spec.components` can reference a copied cross-root Component even when Flux
  creates the directory's Kustomization. Kustomize replacements from a local-config
  ConfigMap can target image fields and env metadata without an applied ConfigMap;
  the recorded experiment verified the local-config object was omitted from output.

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
