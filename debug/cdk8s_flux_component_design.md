# Draft: cdk8s Flux component design

Proposed target shape for the cdk8s assembly spine. The code is an illustrative design sketch, not an executed implementation. The example uses two small components, each owning a Namespace and ConfigMap, to make construction and output ownership visible. Deployments, Services and custom resources would be constructed under the same resource charts. The paths below describe the example layout, not a migration of the current bootstrap.

**The root Flux Kustomization is explicitly constructed. The component Kustomization manifests are collected by cdk8s chart synthesis. Artifact specs are collected in a Python list.** These are three separate operations.

## Common setup: a component owns its subtree

Imports used across the snippets:

```python
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, Yaml
from cdk8s_plus_34 import ConfigMap, Namespace
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator,
    ArtifactGeneratorSpec,
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
    ArtifactGeneratorSpecSources,
    ArtifactGeneratorSpecSourcesKind,
)

FLUX_NAMESPACE = "ducktape-flux"
CONTROL_DIR = Path("cluster/k8s/generated/control")
```

The base class creates a nested resource chart, registers its destination and artifact spec, and exposes the derived source reference. Its method for creating a Kustomization accepts the complete native Flux spec so all operational fields remain available.

```python
class DucktapeFluxComponent(Construct):
    def __init__(
        self,
        scope: Chart,
        name: str,
        *,
        directory: Path,
        artifacts: list[ArtifactGeneratorSpecArtifacts],
        outputs: dict[Chart, Path],
    ):
        super().__init__(scope, name)
        self.component_name = name
        self.resources = Chart(self, "resources")
        outputs[self.resources] = directory / "resources.k8s.yaml"

        self.artifact = ArtifactGeneratorSpecArtifacts(
            name=name,
            origin_revision="@repo",
            copy=[
                ArtifactGeneratorSpecArtifactsCopy(
                    from_=f"@repo/{directory.as_posix()}/**",
                    to="@artifact/",
                )
            ],
        )
        artifacts.append(self.artifact)
        self.source_ref = KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT,
            name=self.artifact.name,
            namespace=FLUX_NAMESPACE,
        )

    def add_kustomization(self, spec: KustomizationSpec) -> Kustomization:
        return Kustomization(
            self,  # A child of this component, inside the enclosing Flux chart.
            "reconciliation",
            metadata=ApiObjectMetadata(
                name=self.component_name,
                namespace=FLUX_NAMESPACE,
            ),
            spec=spec,
        )
```

## A component builds its resources, then its reconciliation

There is no overridden method invoked from the base constructor. The subclass finishes its resource construction before creating its Flux Kustomization.

```python
class ExampleComponent(DucktapeFluxComponent):
    def __init__(
        self,
        scope: Chart,
        name: str,
        *,
        artifacts: list[ArtifactGeneratorSpecArtifacts],
        outputs: dict[Chart, Path],
    ):
        super().__init__(
            scope,
            name,
            directory=Path("cluster/k8s/generated/workloads") / name,
            artifacts=artifacts,
            outputs=outputs,
        )
        namespace = Namespace(
            self.resources,
            "namespace",
            metadata=ApiObjectMetadata(name=name),
        )
        ConfigMap(
            self.resources,
            "settings",
            metadata=ApiObjectMetadata(
                name="settings", namespace=namespace.name
            ),
            data={"message": f"Hello from {name}"},
        )
        self.reconciliation = self.add_kustomization(
            KustomizationSpec(
                source_ref=self.source_ref,
                path="./",
                interval="10m",
                retry_interval="1m",
                timeout="5m",
                prune=True,
                wait=True,
            )
        )
```

For a real component with prerequisites, its constructor takes the specific predecessor Kustomizations as keyword arguments and derives `spec.depends_on` from them. A caller passes `provider.reconciliation`. For example, emitting a Certificate requires the cert-manager provider; emitting a ServiceMonitor requires its CRD provider. Runtime use of a gateway alone does not establish such an edge. The dependency validator checks resource-derived prerequisites instead of requiring runtime services to be healthy based on application names.

## The spine constructs the components, shared AG, and root Flux object

The example assumes the existing bootstrap has already installed Flux and source-watcher, created `ducktape-flux`, and created a `GitRepository` named `ducktape` in that namespace. Its source artifact must include the example control and workload paths. Controller installation and credentials are outside this miniature example.

```python
def generate(repo_root: Path) -> None:
    app = App()
    bootstrap_chart = Chart(app, "bootstrap")
    flux_chart = Chart(app, "flux")
    artifacts_chart = Chart(app, "artifacts")

    outputs: dict[Chart, Path] = {
        bootstrap_chart: Path("cluster/k8s/generated/bootstrap/root.k8s.yaml"),
        flux_chart: CONTROL_DIR / "kustomizations.k8s.yaml",
        artifacts_chart: CONTROL_DIR / "artifact-generators.k8s.yaml",
    }
    artifacts: list[ArtifactGeneratorSpecArtifacts] = []

    alpha = ExampleComponent(
        flux_chart, "example-alpha", artifacts=artifacts, outputs=outputs
    )
    beta = ExampleComponent(
        flux_chart, "example-beta", artifacts=artifacts, outputs=outputs
    )

    ArtifactGenerator(
        artifacts_chart,
        "ducktape-artifacts",
        metadata=ApiObjectMetadata(
            name="ducktape-artifacts", namespace=FLUX_NAMESPACE
        ),
        spec=ArtifactGeneratorSpec(
            sources=[
                ArtifactGeneratorSpecSources(
                    alias="repo",
                    kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY,
                    name="ducktape",
                    namespace=FLUX_NAMESPACE,
                )
            ],
            artifacts=artifacts,  # Explicit collection of plain artifact specs.
        ),
    )

    root_reconciliation = Kustomization(
        bootstrap_chart,
        "root",
        metadata=ApiObjectMetadata(
            name="ducktape-root", namespace=FLUX_NAMESPACE
        ),
        spec=KustomizationSpec(
            source_ref=KustomizationSpecSourceRef(
                kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY,
                name="ducktape",
                namespace=FLUX_NAMESPACE,
            ),
            path=f"./{CONTROL_DIR.as_posix()}",
            interval="10m",
            retry_interval="1m",
            timeout="5m",
            prune=True,
            wait=False,
        ),
    )

    # Construction is complete before any chart is rendered.
    for chart, relative_path in outputs.items():
        destination = repo_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(Yaml.stringify(*chart.to_json()))
```

`alpha`, `beta` and `root_reconciliation` are assigned to show the available handles; the writer never reads those variables. Each constructor has already attached its objects to the construct tree. `chart.to_json()` walks that tree and includes API objects whose nearest enclosing Chart is that chart, including through intermediate component Constructs. It excludes API objects inside nested resource Charts. See the cdk8s [`chartToKube` implementation](https://github.com/cdk8s-team/cdk8s-core/blob/master/src/app.ts).

This writer uses explicit destination paths instead of stock `app.synth()` filenames. It does not flatten `app.synth_yaml()` and repartition the documents afterward.

## What is constructed and written

```text
App
├── bootstrap: Chart
│   └── ducktape-root: Flux Kustomization
├── flux: Chart
│   ├── example-alpha: ExampleComponent
│   │   ├── resources: Chart
│   │   │   ├── Namespace example-alpha
│   │   │   └── ConfigMap example-alpha/settings
│   │   └── reconciliation: Flux Kustomization example-alpha
│   └── example-beta: ExampleComponent
│       ├── resources: Chart
│       │   ├── Namespace example-beta
│       │   └── ConfigMap example-beta/settings
│       └── reconciliation: Flux Kustomization example-beta
└── artifacts: Chart
    └── ArtifactGenerator ducktape-artifacts
```

| Generated file, relative to `cluster/k8s/generated/` | Contents                                                                              |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------- |
| `bootstrap/root.k8s.yaml`                            | The explicitly constructed `ducktape-root` Flux Kustomization                         |
| `control/kustomizations.k8s.yaml`                    | `alpha.reconciliation` and `beta.reconciliation`, collected by cdk8s through the tree |
| `control/artifact-generators.k8s.yaml`               | One AG with the two specs from `artifacts`                                            |
| `workloads/example-alpha/resources.k8s.yaml`         | Alpha's Namespace and ConfigMap                                                       |
| `workloads/example-beta/resources.k8s.yaml`          | Beta's Namespace and ConfigMap                                                        |

## How Flux reaches everything at runtime

The existing bootstrap applies or includes `bootstrap/root.k8s.yaml`. The root object then reconciles the `control/` directory from the GitRepository. That directory contains the generated child Flux Kustomizations and shared AG. Its simple YAML-only contents allow Flux to generate the ordinary Kustomize build file automatically; a checked-in `kustomization.yaml` can explicitly list the two files if desired. [Flux path behavior](https://fluxcd.io/flux/components/kustomize/kustomizations/#path).

```text
existing bootstrap
  → applies ducktape-root
      → builds GitRepository path ./cluster/k8s/generated/control
          → applies shared ArtifactGenerator
              → produces ExternalArtifacts example-alpha and example-beta
          → applies child Flux Kustomizations example-alpha and example-beta
              → each builds path ./ inside its own ExternalArtifact
                  → applies its Namespace and ConfigMap
```

The root object's spec never contains `[alpha.reconciliation, beta.reconciliation]`. Its `path` selects the control directory. The shared Flux chart's output contains those two objects because of their construct scopes. The root uses `wait=False` and has no `dependsOn` edges to its children; children can retry while their ExternalArtifacts are being produced. Bootstrap owns the root object itself, which lives outside its reconciled `control/` directory.

## Artifact grouping

Start with one AG per source group. In the repository's source-watcher `v2.2.4` pin, each generator downloads and extracts its inputs for a source-change reconciliation, then processes its outputs sequentially. Splitting one source across N generators repeats source fetching and generator status updates N times. Both arrangements still produce N ExternalArtifacts. The controller patches each output's status with a fresh condition timestamp even when its content digest is unchanged. [Controller implementation](https://github.com/fluxcd/source-watcher/blob/v2.2.4/internal/controller/artifactgenerator_controller.go).

Fewer AG status writes do not mean a proportional reduction in etcd bytes: a status update persists the resulting whole object, including the artifact list and inventory. A shared AG has fewer, larger writes; separate AGs repeat object metadata and source declarations. These are deductions from controller and storage behavior, not measured disk I/O. [Kubernetes storage implementation](https://github.com/kubernetes/apiserver/blob/master/pkg/storage/etcd3/store.go).

The tradeoff is failure isolation. A shared AG returns on an output failure, leaving earlier outputs potentially updated and later outputs unprocessed; separate AGs can reconcile independently. Keep the collector separate from component construction so grouping can change without changing dependency signatures.

## Adoption boundaries

Preserve explicit component prerequisites and keep one component per Flux reconciliation unit where the DAG needs interleaving, such as cert-manager operator and environment units around external-secrets. Image pins, SOPS inputs and shared-base packaging stay explicit.

Adopting this shape requires revising the entry-point-only join convention in <../cluster/cdk8s/AGENTS.md> and replacing output routing based on Flux `spec.path`. It also changes construct paths, so preserve deployed resource identities explicitly when converting existing charts. Ownership moves and bootstrap path changes need their own focused review. The example does not authorize or implement those migrations.
