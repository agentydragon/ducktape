"""Dex and the acceptance client must consume the same generated password."""

from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one


def test_dex_config_and_operator_password_share_one_generator_invocation(k8s_dir: Path) -> None:
    root = k8s_dir / "agentplane-testing/dex"
    resources = list(yaml.safe_load_all((root / "agentplane-testing-dex.k8s.yaml").read_text()))
    secrets = [resource for resource in resources if resource["kind"] == "ExternalSecret"]
    operator = one(secret for secret in secrets if "password" in secret["spec"]["target"]["template"]["data"])
    target = operator["spec"]["target"]
    template = target["template"]
    config = yaml.safe_load(template["data"]["config.yaml"])
    identity = one(config["staticPasswords"])
    assert template["data"]["login"] == identity["email"]
    assert template["data"]["username"] == identity["preferredUsername"]
    assert template["data"]["password"] == "{{ .password }}"
    assert ".password" in identity["hash"]
    generator = one(operator["spec"]["dataFrom"])["sourceRef"]["generatorRef"]
    # Referring to the same Password resource twice does not share its output.
    invocations = [source["sourceRef"]["generatorRef"] for secret in secrets for source in secret["spec"]["dataFrom"]]
    assert invocations.count(generator) == 1

    deployment = one(resource for resource in resources if resource["kind"] == "Deployment")
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
