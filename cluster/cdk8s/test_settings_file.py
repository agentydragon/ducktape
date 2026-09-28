"""`SettingsFile.mount_into` runs after `add_container` returns, through cdk8s-plus's own `mount`
and `env.add_variable`; the rendered pod spec still resolves the env var to the ConfigMap key."""

from pathlib import PurePosixPath

import pytest_bazel
import yaml
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from cdk8s_plus_34 import Deployment
from more_itertools import one
from pydantic import BaseModel

from cluster.cdk8s.settings_file import SettingsFile


class _Settings(BaseModel):
    listen_port: int
    allowed_namespaces: list[str]


def test_the_env_var_names_the_mounted_file() -> None:
    chart = Chart(Cdk8sTesting.app(), "test")
    content = {"listen_port": 8080, "allowed_namespaces": ["settings-file-test"]}
    settings = SettingsFile(
        chart,
        "settings",
        metadata=ApiObjectMetadata(name="app-settings"),
        model=_Settings,
        content=content,
        path="/etc/app/app.yaml",
    )
    settings.mount_into(Deployment(chart, "deployment").add_container(image="app.example.test/app"), env="APP_CONFIG")

    objects = Cdk8sTesting.synth(chart)
    pod = one(o for o in objects if o["kind"] == "Deployment")["spec"]["template"]["spec"]
    container = one(pod["containers"])
    path = PurePosixPath(one(var["value"] for var in container["env"] if var["name"] == "APP_CONFIG"))
    mount = one(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(path.parent))
    assert mount["readOnly"] is True
    config_map = one(o for o in objects if o["kind"] == "ConfigMap")
    assert one(v for v in pod["volumes"] if v["name"] == mount["name"])["configMap"]["name"] == "app-settings"
    assert yaml.safe_load(config_map["data"][path.name]) == content


if __name__ == "__main__":
    pytest_bazel.main()
