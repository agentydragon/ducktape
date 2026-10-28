"""Ingress file settings and environment references form one valid runtime configuration."""

from typing import Any

import protovalidate
import pytest_bazel
import yaml
from google.protobuf.json_format import ParseDict
from more_itertools import one

from agentplane.llm_ingress.configpb import config_pb2
from cluster.cdk8s.agentplane import staging


def test_rendered_ingress_config_is_completed_by_environment_and_secret(
    staging_manifests: list[dict[str, Any]],
) -> None:
    deployment = one(
        doc for doc in staging_manifests if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "llm-ingress"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    environment = {entry["name"]: entry for entry in container["env"]}
    prefix = "AGENTPLANE_LLM_INGRESS_"
    path = environment[prefix + "CONFIG_FILE"]["value"]
    mount = one(mount for mount in container["volumeMounts"] if path.startswith(mount["mountPath"] + "/"))
    volume = one(volume for volume in pod["volumes"] if volume["name"] == mount["name"])
    config_map = one(
        doc
        for doc in staging_manifests
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == volume["configMap"]["name"]
    )
    content = yaml.safe_load(config_map["data"][path.rsplit("/", 1)[1]])
    assert "litellm_key" not in content
    assert environment[prefix + "LITELLM_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": staging.ENV.llm_ingress.litellm_key_secret_name,
        "key": "api-key",
    }
    config = ParseDict(content, config_pb2.Config())
    ParseDict(
        {
            name.removeprefix(prefix).lower(): entry["value"]
            for name, entry in environment.items()
            if name.startswith(prefix) and name != prefix + "CONFIG_FILE" and "value" in entry
        },
        config,
    )
    config.litellm_key = "test-secret-supplied-at-runtime"
    protovalidate.validate(config)


if __name__ == "__main__":
    pytest_bazel.main()
