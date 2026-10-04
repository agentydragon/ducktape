"""Headlamp plugin-manager config follows the documented plugin configuration format."""

import pytest_bazel
import yaml

from cluster.cdk8s.headlamp import _PLUGINS_CONFIG, _HeadlampPlugin, _PluginInstallOptions


def test_plugin_manager_config_matches_headlamp_contract() -> None:
    # Headlamp's field contract: https://github.com/kubernetes-sigs/headlamp/blob/main/docs/installation/in-cluster/index.md#plugin-configuration-format
    plugin_fields = set(_HeadlampPlugin.model_json_schema(by_alias=True)["properties"])
    assert plugin_fields == {"name", "source", "version", "dependencies"}

    option_fields = set(_PluginInstallOptions.model_json_schema(by_alias=True, mode="serialization")["properties"])
    assert option_fields == {"parallel", "maxConcurrent"}

    config = yaml.safe_load(_PLUGINS_CONFIG)
    assert set(config) == {"plugins", "installOptions"}
    for plugin in config["plugins"]:
        assert set(plugin) == {"name", "source", "version"}


if __name__ == "__main__":
    pytest_bazel.main()
