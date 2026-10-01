"""Headlamp plugin-manager config follows the documented plugin configuration format."""

import pytest_bazel
import yaml

from cluster.cdk8s.headlamp import _PLUGINS_CONFIG, _HeadlampPlugin, _PluginInstallOptions, _PluginsConfig


def test_plugin_manager_config_matches_headlamp_contract() -> None:
    # Headlamp's field contract: https://github.com/kubernetes-sigs/headlamp/blob/main/docs/installation/in-cluster/index.md#plugin-configuration-format
    plugin_fields = set(_HeadlampPlugin.model_json_schema(by_alias=True)["properties"])
    assert plugin_fields == {"name", "source", "version", "dependencies"}
    assert not _HeadlampPlugin.model_fields["dependencies"].is_required()

    option_fields = set(_PluginInstallOptions.model_json_schema(by_alias=True, mode="serialization")["properties"])
    assert option_fields == {"parallel", "maxConcurrent"}
    assert not _PluginInstallOptions.model_fields["parallel"].is_required()
    assert not _PluginInstallOptions.model_fields["max_concurrent"].is_required()
    assert not _PluginsConfig.model_fields["install_options"].is_required()

    config = yaml.safe_load(_PLUGINS_CONFIG)
    assert set(config) == {"plugins", "installOptions"}
    assert config["installOptions"] == {"parallel": True, "maxConcurrent": 2}
    assert len(config["plugins"]) == 5
    for plugin in config["plugins"]:
        assert set(plugin) == {"name", "source", "version"}
        assert all(isinstance(plugin[field], str) for field in ("name", "source", "version"))


if __name__ == "__main__":
    pytest_bazel.main()
