"""`SettingsFile.mount_into` runs after `add_container` returns, through cdk8s-plus's own `mount`
and `env.add_variable`; the rendered pod spec still resolves the env var to the ConfigMap key."""

from pathlib import PurePosixPath

import pytest_bazel
from cdk8s import ApiObject, ApiObjectMetadata, Chart
from cdk8s_plus_34 import Deployment
from more_itertools import one

from cluster.cdk8s.settings_file import SettingsFile


def test_the_env_var_names_the_mounted_file(scratch_chart: Chart) -> None:
    content = "listen_port: 8080\nallowed_namespaces:\n  - settings-file-test\n"
    settings = SettingsFile(
        scratch_chart, "settings", metadata=ApiObjectMetadata(), content=content, path="/etc/app/app.yaml"
    )
    deployment = Deployment(scratch_chart, "deployment")
    settings.mount_into(deployment.add_container(image="app.example.test/app"), env="APP_CONFIG")

    pod = ApiObject.of(deployment).to_json()["spec"]["template"]["spec"]
    container = one(pod["containers"])
    path = PurePosixPath(one(var["value"] for var in container["env"] if var["name"] == "APP_CONFIG"))
    mount = one(mount for mount in container["volumeMounts"] if mount["mountPath"] == str(path.parent))
    assert mount["readOnly"] is True
    config_map = ApiObject.of(settings.config_map).to_json()
    assert (
        one(v for v in pod["volumes"] if v["name"] == mount["name"])["configMap"]["name"]
        == config_map["metadata"]["name"]
    )
    assert config_map["data"][path.name] == content


if __name__ == "__main__":
    pytest_bazel.main()
