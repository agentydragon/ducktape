"""`SettingsFile`: an in-repo binary's settings file, rendered from its pydantic Settings model."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import Any

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, Container, EnvValue, Volume
from constructs import Construct
from pydantic import BaseModel

from cluster.cdk8s.config_format import yaml_config
from util.settings_contract import settings_file


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

    def mount_into(self, container: Container, *, env: str) -> None:
        container.mount(str(self._path.parent), Volume.from_config_map(self, "volume", self.config_map), read_only=True)
        container.env.add_variable(env, EnvValue.from_value(str(self._path)))
