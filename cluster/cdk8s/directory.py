"""One Flux Kustomization directory: what it renders and what it waits on. Shared by
every fully generated directory (litellm/app, ha-mcp, clickhouse/schema, aiquota,
agentplane-{staging,testing}, haku/console{,/db,/migration}, monitoring/etcd) and by the
directories that generate only their `.k8s.yaml` files inside an otherwise hand-written
Kustomization (`generate_flux=False`); `generate_manifests.py` walks one roster of these
instead of a bespoke `_generate_*` function per directory. See cluster/docs/cdk8s.md
§ Three shapes of a directory.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field

from cdk8s import App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
    KustomizationSpecHealthChecks,
)

from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux_constructs import ConfigMapArgs, health_checks


@dataclass(frozen=True)
class Directory:
    """One Flux Kustomization directory: what it renders and what it waits on.

    Builds its chart one of two ways: `populate` adds constructs to a chart this module
    creates and names `name` (the common case); `build` creates and names the chart
    itself, for the few directories whose chart id isn't the Kustomization name
    (`agentplane`'s environments) or that generate more than one chart file into a
    hand-written directory (`generate_flux=False`).
    """

    # The Flux Kustomization name and the directory it renders into.
    name: str
    path: str
    populate: Callable[[Chart], object] | None = None
    build: Callable[[App], Chart] | None = None
    depends_on: tuple[str, ...] = ()
    # Secrets and ConfigMaps Pods read that the chart does not create, each with the
    # dependency or sibling file that does; fleet_rules checks both ends.
    provided_secrets: Mapping[str, str] = field(default_factory=dict)
    provided_config_maps: Mapping[str, str] = field(default_factory=dict)
    # CiliumNetworkPolicies allowed unpinned (SNI-less) HTTPS egress: TLS-terminating
    # proxies whose allowlist is their own configuration, not this policy's serverNames.
    unpinned_https_egress: Collection[str] = ()
    # Hand-written files listed beside the generated one(s).
    extra_resources: tuple[str, ...] = ()
    config_map_generator: tuple[ConfigMapArgs, ...] = ()
    # Whether a hand-written image-pins/ Component overrides the placeholder image tags.
    image_pins: bool = False
    # The kustomization.yaml `namespace`, which the configMapGenerator output needs.
    namespace: str | None = None
    # Kinds whose readiness the Flux Kustomization lists explicitly on top of `wait: true`.
    health_check_kinds: tuple[str, ...] = ()
    # Health checks the chart's own rendered objects don't carry directly: a target this
    # directory depends on (ha-mcp's home-assistant Job) or a controller's async write
    # target (agentplane's trust-manager Bundle ConfigMaps).
    extra_health_checks: Callable[[Chart], Sequence[KustomizationSpecHealthChecks]] | None = None
    health_check_exprs: tuple[KustomizationSpecHealthCheckExprs, ...] = ()

    # Whether this directory generates its own `flux-kustomization.yaml` and
    # `kustomization.yaml` (shape 1) or only the chart file(s) beside a hand-written pair
    # (shape 2, cluster/docs/cdk8s.md).
    generate_flux: bool = True
    description: str | None = None
    interval: str = "10m"
    retry_interval: str | None = "1m"
    timeout: str = "10m"
    wait: bool | None = True
    deletion_policy: KustomizationSpecDeletionPolicy | None = None
    # The `ExternalArtifact` source ref name, when it differs from `name`.
    source_ref_name: str | None = None

    def __post_init__(self) -> None:
        if (self.populate is None) == (self.build is None):
            raise ValueError(f"Directory {self.name!r} needs exactly one of populate= or build=")

    def make_chart(self, app: App) -> Chart:
        if self.build is not None:
            return self.build(app)
        assert self.populate is not None
        chart = Chart(app, self.name, disable_resource_name_hashes=True)
        self.populate(chart)
        return chart

    def health_checks(self, chart: Chart) -> list[KustomizationSpecHealthChecks]:
        checks = health_checks(chart, self.health_check_kinds)
        if self.extra_health_checks is not None:
            checks = [*checks, *self.extra_health_checks(chart)]
        return checks


def chart(app: App, directory: Directory) -> Chart:
    """Build `directory`'s chart with fleet rules attached -- shared by the generator
    (writes it to disk) and tests that synthesize a directory's chart in memory."""
    built = directory.make_chart(app)
    add_fleet_rules(
        built,
        provided_secrets=directory.provided_secrets,
        provided_config_maps={
            # A `config_map_generator` entry always provides its own ConfigMap: kustomize
            # renders it from exactly the files listed, so a caller never has to restate
            # the relation `directory.provided_config_maps` exists for.
            **{generator.name: generator.files[0] for generator in directory.config_map_generator},
            **directory.provided_config_maps,
        },
        providers=frozenset(
            {
                # A directory may always cite itself as the provider of a Secret one of its
                # own Jobs creates imperatively at runtime (ha-mcp's token provisioner),
                # which no static object in the rendered chart represents.
                directory.name,
                *directory.depends_on,
                *directory.extra_resources,
                *(file for generator in directory.config_map_generator for file in generator.files),
            }
        ),
        unpinned_https_egress=directory.unpinned_https_egress,
    )
    return built
