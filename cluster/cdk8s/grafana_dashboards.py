"""Grafana dashboards whose JSON is a native file beside the generator module that deploys them,
packaged by the directory's generated `kustomization.yaml` as a `configMapGenerator` ConfigMap.
Its content-hash name suffix reaches the `GrafanaDashboard`'s `configMapRef`, so an edited
dashboard is a new ConfigMap that grafana-operator re-imports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from grafana_grafanadashboard_crds.org.integreatly.grafana import GrafanaDashboard, GrafanaDashboardSpecConfigMapRef

from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.generation import copy_source_file, write_yaml

_KUSTOMIZE_CONFIG = "kustomizeconfig"


@dataclass(frozen=True)
class DashboardFile:
    """The dashboard JSON at repo-relative `source`, deployed as ConfigMap `config_map` in
    `namespace` under its own file name."""

    source: str
    config_map: str
    namespace: str

    def config_map_ref(self) -> GrafanaDashboardSpecConfigMapRef:
        return GrafanaDashboardSpecConfigMapRef(name=self.config_map, key=PurePosixPath(self.source).name)


def config_map_generator(root: Path, directory: str, dashboards: Sequence[DashboardFile]) -> list[ConfigMapArgs]:
    """Copy each dashboard's JSON into `directory`; return the `configMapGenerator` entries."""
    return [
        ConfigMapArgs(
            name=dashboard.config_map,
            namespace=dashboard.namespace,
            files=[copy_source_file(root, directory, dashboard.source)],
        )
        for dashboard in dashboards
    ]


def write_kustomize_config(root: Path, directory: str) -> str:
    """Write into `directory` the `nameReference` that points a `GrafanaDashboard`'s
    `configMapRef` at its ConfigMap's generated name, which Kustomize's built-in references (core
    kinds only) miss; return its file name for `configurations:`."""
    group, version = GrafanaDashboard.GVK.api_version.split("/")
    write_yaml(
        root / directory / _KUSTOMIZE_CONFIG,
        {
            "nameReference": [
                {
                    "kind": "ConfigMap",
                    "version": "v1",
                    "fieldSpecs": [
                        {
                            "group": group,
                            "version": version,
                            "kind": GrafanaDashboard.GVK.kind,
                            "path": "spec/configMapRef/name",
                        }
                    ],
                }
            ]
        },
    )
    return _KUSTOMIZE_CONFIG
