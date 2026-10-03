# cdk8s for cluster manifests

Python cdk8s (`cluster/cdk8s/`) generates resources under two roots
(`cluster/cdk8s/manifest_roots.py`), mirroring one sub-path layout. A Flux Kustomization
directory every file of which is generated lives under `cluster/generated`, a closed
world: the parity test fails on any file there the generator does not write. A directory
holding any hand-written file lives under `cluster/k8s`, its generated files beside the
hand-written ones; the exception is an `image-pins` Component, which may be a
`cluster/k8s` directory of its own that a generated directory includes across the roots
([the mixed-directory rule](cdk8s_remainder.md#mixed-directory-layout)). A directory is
never split across the roots, and never nested inside another Kustomization's path or
artifact copy in the other root. The Flux Kustomization graph is one generated chart at
`cluster/k8s/flux/kustomizations.k8s.yaml`;
the neighboring `flux/kustomization.yaml` keeps bootstrap and source objects hand-written.
Flux reads `devel` as it always has. Regenerate with `bb run //cluster/cdk8s:generate_manifests`
(the binary writes into the checkout, so `bb run`, not `bbr run`);
`//cluster/cdk8s:test_generate_manifests` regenerates in memory and fails CI when a
written file differs from, or is missing at, its committed path, or when `cluster/generated`
holds a file the generator did not write. Conventions for writing a generator: <../cdk8s/AGENTS.md>.

## Shapes of a directory

Most active Kubernetes resources under these two roots are generated. Hand-written
overlays, application payloads and project-owned deployment packages remain; their
treatment is tracked in [the remainder backlog](cdk8s_remainder.md).
`cluster/cdk8s/render_diff.py` compares the final Kustomize resources across revisions,
before postBuild substitution (<../cdk8s/AGENTS.md> § Testing a generator).

**One generated file per directory.** A directory's generated resources are all in
`<directory name>.k8s.yaml`: its one writer, `generation.write_charts` (which
`write_directory` calls), synthesizes every chart of the directory into that file, in chart
order. A module whose objects belong in another module's directory exports a chart builder
that directory's writer includes (`egress_fences.py`, `agents/namespaces.py`), never a
second file. Beside it sit only `patches.k8s.yaml`, the strategic-merge patches
`write_directory` lists under `patches:`, and one file a consumer outside Kubernetes reads:
`talos-cloud-controller-manager/helmrelease.k8s.yaml`, the lone HelmRelease that
`cluster/terraform/main/talos-ccm.tf` `yamldecode`s, which takes a single document. The
central Flux chart keeps its name, `flux/kustomizations.k8s.yaml`.

1. **No `kustomization.yaml`**, in a generated directory not yet written by
   `generation.write_directory`: kustomize-controller generates the kustomization, listing
   every `.yaml`/`.yml` file under the path recursively and a subdirectory holding a
   kustomization as a whole (fluxcd/pkg `kustomize.scanManifests`). A directory another
   kustomization references as a resource keeps its file; kustomize requires one there.
2. **Generated `kustomization.yaml`** (every one `.gitattributes` marks
   `linguist-generated=true`): `flux.kustomize_kustomization`, a Pydantic model (the plain
   `kustomize.config.k8s.io` Kustomization has a JSON Schema but no CRD for
   `cdk8s import` to ingest), listing the generated file and the hand-written siblings
   below. `generation.write_directory` writes it for every
   directory it synthesizes, and a new component is written that way. A pinned upstream
   release manifest is a remote resource the kustomization names by URL, with patches built
   in Python (`kubevirt/operators.py`); its objects are never transcribed into constructs.
3. **Hand-written `kustomization.yaml` over generated resources**, where the directory
   keeps something the generator does not own (a `configMapGenerator` with
   `configurations:` or `generatorOptions`, an object from § What stays hand-written).
   It lists the generated file as a resource with a comment naming the generator
   modules.
4. **Hand-written outright**: `flux/flux-system` (`flux bootstrap` output).

The parked tree, `cluster/parked` (`PARKED_ROOT`), is outside both roots and applied by
nothing; it is hand-written apart from augur-evidence's generated file.

Every directory's Flux Kustomization object is created in topo order by
`generate_manifests.py` and emitted in the central Flux chart. `artifact-generators`
imports the deployed source-watcher CRD; `generate_manifests.py` builds each consumer's
artifact before its Kustomization node, writes the directory with
`generation.write_directory` as the node's `RenderedDirectory` argument, and hands every
artifact to `artifact_generators.write_artifact_generators` last. A tofu-controller
`Terraform` CR is built through `terraform.tofu_state_terraform` (a `tf/gitops` module's
through `terraform.gitops_terraform`), and a Namespace in the directory of the
Kustomization that owns it through `generation.namespace_chart`.

### What stays hand-written

- `.sops.yaml` Secrets (cdk8s has no key material; below).
- Vendored and externally generated manifests: `flux/flux-system`.
- `configMapGenerator` inputs (Iron and app configs, SQL, blueprints), and the
  `kustomization.yaml` that carries the generator where it is hand-written.
- `image-pins/` Components and the ConfigMaps whose data carries a `$imagepolicy` marker
  (§ Live image automation).

Open work is in <../cdk8s/PLAN.md>; file-specific treatment and mixed-directory
mechanism findings are in <cdk8s_remainder.md>.

The `.k8s.yaml` suffix is cdk8s-only and Prettier ignores it so synthesis retains
ownership of generated bytes; `.gitattributes` marks every generated file
`linguist-generated=true`. The one central Flux chart has no per-directory
`flux-kustomization.yaml` output.

A value a `tf/gitops` module takes from the generators reaches it as an inline
`spec.vars` entry on its generated CR (`litellm/keys-tf`'s `model_allowlists`, the
per-key lanes `cluster/cdk8s/litellm/keys.py` derives from the roster; `dns-automation`'s
`public_nodes`, the mesh roster's projection), never as a generated file beside the
module: the tofu-controller's `ducktape` GitRepository is a sparse checkout of deployment
directories, so a module cannot `file()` a repo-root input, and `vars[].value` is written
structurally into the runner's tfvars, so a map arrives typed, and a CR spec change
reconciles immediately, whereas `varsFrom` ConfigMap values are stringified and picked
up only on the interval (tofu-controller v0.16.5).

### SOPS secrets in a converted directory

A `.sops.yaml` Secret stays hand-written (cdk8s has no key material) in the same
directory: the generated `kustomization.yaml` lists it as a sibling resource
(`write_directory(..., siblings=[...])`, agentplane's `Environment.extra_resources`), and
that directory's object in the central Flux chart carries the `decryption:` block when any
such file is listed; `flux_kustomization` derives it from the `RenderedDirectory`.
`ExternalSecret` objects are generated outright; they carry only a pointer at a store key.

Fleet rules run only on charts registering `add_fleet_rules`. Their
`resolved_references` check rejects some same-chart Secret/ConfigMap kind mismatches,
including names derived from controller-created targets; it permits unknown references
and does not distinguish namespaces. It does not establish reference existence.
Whole-tree validation work is tracked in <../cdk8s/TODO.md>.

A `configMapGenerator` input (`aiquota/schema.sql`)
stays hand-written the same way: the generated `kustomization.yaml` carries the
generator entry (`flux.ConfigMapArgs`), keeping kustomize's content-hash
name suffix and reference rewriting, and the construct mounting it references the
entry's `name`. The input may instead sit beside its generator module, which copies it
into the output directory (`generation.copy_source_file`), so the directory can live
under `cluster/generated` (the Grafana dashboards, `grafana_dashboards.py`). Content
rendered in Python goes into the same entry as `literals` (aiquota's `config.toml`).
Kustomize rewrites a generated name only into fields it knows; a custom resource's
reference (`GrafanaDashboard.spec.configMapRef.name`) needs a `nameReference` transformer
configuration listed under `configurations:`.

### `dependsOn` rationale

cdk8s emits no YAML comments (below), so a dependency's reason lives as a Python
comment next to the `depends_on` entry.

## Mixing with hand-written Flux manifests

The root `cluster/k8s/kustomization.yaml` includes `flux/`; the hand-written
`cluster/k8s/flux/kustomization.yaml` includes the generated central chart and the
hand-written bootstrap/source resources. `dependsOn` addresses Kustomizations by name;
`cluster/validation`'s graph checks (`test_dependencies`, `test_crd_layering`,
`test_flux_build`) run over rendered `kustomize build` output of both roots; kubeconform
validates every `cluster/{k8s,generated}/**/*.yaml`.

## One writer per byte range

A GitOps controller's write-back and the generator must never both own the same
committed bytes; whichever writes second silently wins.

### Live image automation

Flux's `ImageUpdateAutomation` rewrites a `# {"$imagepolicy": ...}` marker comment in
git. cdk8s's model is JSON-shaped data serialized to YAML last, with no node for a
comment, and Flux's Setters strategy is the only one it ships, so the marker cannot
live in a generated file. The carve-out is at field granularity: every generated
Deployment carries the placeholder tag `unset`, and a hand-written
`image-pins/kustomization.yaml` Kustomize `Component` (referenced by the generated
`kustomization.yaml`'s `components:`) carries the marker and overrides the tag at
`kustomize build` time. The marker form and its incident: <../cdk8s/AGENTS.md>
§ `image-pins/kustomization.yaml`. A test asserts no generated file contains
`$imagepolicy`. Where a tag is also data a Pod reads (the console reports its own and its
static shell's image tags), it lives in a hand-written sibling ConfigMap carrying the
marker (`haku/console/{image,static}-metadata.yaml`), listed as a resource in the root
Kustomization and referenced by name from the workload. A KubeVirt `VirtualMachine`'s
`containerDisk` image is outside the `images:` transformer's default field specs, so its
Component also lists a `kustomizeconfig` naming that path (`cpap-sync/image-pins`).

Argo CD Image Updater would need the identical carve-out: its `git` write-back mode
writes a separate file, and its default `argocd` mode stores the override on the live
`Application` object only, which a bootstrap from committed state loses (contra
`cluster/AGENTS.md` § Primary Directive). Image automation therefore moves the
Flux-vs-Argo question in neither direction.

## CRD bindings

`devinfra/js/cdk8s_import.bzl` wraps `cdk8s import`: for most providers, the CRD YAML is
an `http_file` in `MODULE.bazel` pinned by sha256 to the version the cluster deploys
(the same tag as the operator's `GitRepository` or Terraform install). The jsii-backed
Python bindings are build-time output, never committed. Put each import declaration and
its optional smoke test in `cluster/cdk8s/providers/<provider>/BUILD.bazel`, beside the
CRD's generic wrapper modules; keep upstream CRD source pins in `MODULE.bazel`. Current
providers are
`//cluster/cdk8s/providers/{agent_sandbox,cert_manager,cilium,clickhouse,cnpg,external_secrets,external_snapshotter,flux,gateway_api,grafana_operator,keda,kubevirt,kyverno,prometheus_operator,redis_operator,seaweedfs,source_watcher,tofu_controller,volsync}`.
`//agentplane/crds` declares the imports of Agentplane's first-party CRDs beside their
YAML; `providers/agentplane` holds only their wrappers, and `agentplane_crds.py` points Flux
at the same authored directory under `agentplane/crds/manifests`.

The `source_watcher` import extracts `ArtifactGenerator` from the CRD bundle in
`cluster/k8s/flux/flux-system/gotk-components.yaml`, keeping the binding aligned with the
Flux version deployed by the repository.

- `cdk8s import` takes one CRD per invocation; a bundled multi-document file
  (external-secrets) goes through the `devinfra/k8s/extract_crd.py` genrule first.
  KubeVirt and CDI publish no YAML for the CRDs their operators create at runtime; the
  same genrule extracts them from the generated Go files that embed them
  (`crd_go_key`), wrapping KubeVirt's schema-only entries in a CRD (`crd_wrap_kind`).
- A CRD group with a dash (`external-secrets.io`) keeps it in the jsii assembly's npm
  name but not in the Python package directory; `jsii_module_path` carries the second
  spelling.
- Core kinds need no import: `cdk8s_plus_34`'s fluent layer and its `k8s` submodule
  cover them (<../cdk8s/AGENTS.md> § Typed constructs over raw dicts).

## Toolchain failures

Use the Nix devshell and the repository's Bazel workflow. Missing tools or BuildBuddy
connectivity failures follow the recovery procedure in the root `AGENTS.md`; a
standalone venv is not an alternative generation toolchain.
