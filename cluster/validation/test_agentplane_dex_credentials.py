"""Dex and the acceptance client must consume the same generated password."""

from pathlib import Path
from typing import Any

import pytest_bazel
import yaml
from more_itertools import one

# pytest_plugins loads cluster.validation.agentplane_fixtures by name; gazelle cannot see
# the dependency.
# gazelle:include_dep //cluster/validation:agentplane_fixtures
pytest_plugins = ("cluster.validation.agentplane_fixtures",)


def test_dex_config_and_operator_password_share_one_generator_invocation(
    agentplane_manifests: dict[str, list[dict[str, Any]]],
) -> None:
    resources = agentplane_manifests["agentplane-testing"]
    secrets = [resource for resource in resources if resource["kind"] == "ExternalSecret"]
    operator = one(
        secret for secret in secrets if secret["metadata"]["name"] == "agentplane-testing-acceptance-operator"
    )
    target = operator["spec"]["target"]
    template = target["template"]
    config = yaml.safe_load(template["data"]["config.yaml"])
    identity = one(config["staticPasswords"])
    # login/email and username/preferredUsername come from the same _ACCEPTANCE_EMAIL/
    # _ACCEPTANCE_USERNAME constants in dex_constructs.py, not independently typed strings.
    assert template["data"]["password"] == "{{ .password }}"
    assert ".password" in identity["hash"]
    generator = one(operator["spec"]["dataFrom"])["sourceRef"]["generatorRef"]
    # Referring to the same Password resource twice does not share its output. Other secrets in
    # this merged file (e.g. forgejo-images-creds) pull from a SecretStore `extract`, not a
    # generator, and carry no `sourceRef`.
    invocations = [
        source["sourceRef"]["generatorRef"]
        for secret in secrets
        for source in secret["spec"]["dataFrom"]
        if "sourceRef" in source
    ]
    assert invocations.count(generator) == 1

    deployment = one(
        resource
        for resource in resources
        if resource["kind"] == "Deployment" and resource["metadata"]["name"] == "agentplane-testing-dex"
    )
    pod = deployment["spec"]["template"]["spec"]
    container = one(pod["containers"])
    config_path = Path(container["args"][-1])
    mount = one(mount for mount in container["volumeMounts"] if Path(mount["mountPath"]) == config_path.parent)
    volume = one(volume for volume in pod["volumes"] if volume["name"] == mount["name"])
    assert volume["secret"]["secretName"] == target["name"]
    # Mount only the config, not the plaintext operator credential.
    assert volume["secret"]["items"] == [{"key": "config.yaml", "path": config_path.name}]
    assert target["name"] in deployment["metadata"]["annotations"]["secret.reloader.stakater.com/reload"].split(",")


if __name__ == "__main__":
    pytest_bazel.main()
