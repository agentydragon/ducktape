"""`SettingsFile`: an in-repo binary's settings file, rendered from its pydantic Settings model."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, EnvValue, Volume, k8s
from constructs import Construct
from pydantic import BaseModel

from cluster.cdk8s.config_format import yaml_config
from util.settings_contract import settings_file


@dataclass(frozen=True)
class PodWiring:
    """What a settings file adds to a Pod spec: the volume, the container's mount of it, and the
    environment variable that names the file."""

    volume: k8s.Volume
    mount: k8s.VolumeMount
    path_env: k8s.EnvVar


class SettingsFile(Construct):
    """`content`, each key checked against `model`, as the YAML file the binary reads at `path`.
    ConfigMap `metadata.name` holds it under the file's name; `mount_into` mounts that ConfigMap
    read-only on the file's directory and points the binary's config-file env var at `path`, so the
    key, the mount and the path cannot disagree. `supplied` names the leaves another source (a
    Secret's env var) completes; with it the whole file also validates as `model`."""

    def __init__(
        self,
        scope: Construct,
        id: str,
        *,
        metadata: ApiObjectMetadata,
        model: type[BaseModel],
        content: dict[str, Any],
        path: str,
        supplied: Iterable[Sequence[str]] = (),
    ) -> None:
        super().__init__(scope, id)
        self._path = PurePosixPath(path)
        self.config_map = ConfigMap(
            self,
            "config-map",
            metadata=metadata,
            data={self._path.name: yaml_config(settings_file(model, content, supplied=supplied))},
        )

    def pod_wiring(self, *, volume_name: str, env: str) -> PodWiring:
        """`mount_into` for a container written as core Pod types, as in a Pod template."""
        return PodWiring(
            volume=k8s.Volume(name=volume_name, config_map=k8s.ConfigMapVolumeSource(name=self.config_map.name)),
            mount=k8s.VolumeMount(name=volume_name, mount_path=str(self._path.parent), read_only=True),
            path_env=k8s.EnvVar(name=env, value=str(self._path)),
        )

    def mount_into(self, container: Container, *, env: str) -> None:
        container.mount(str(self._path.parent), Volume.from_config_map(self, "volume", self.config_map), read_only=True)
        container.env.add_variable(env, EnvValue.from_value(str(self._path)))
