"""Verify that the manual VM pilot is isolated and can admit the 10-GB suite."""

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from devinfra.buildbuddy.kvm.x.vm_pilot.render import NAME, render_worker


def test_pilot_from_current_worker_artifact() -> None:
    source = (
        Runfiles.CreateOrRaise().root() / "ducktape/cluster/k8s/buildbuddy-kvm-worker/buildbuddy-kvm-worker.k8s.yaml"
    ).read_text()
    config, vm, policy, service, monitor = yaml.safe_load_all(render_worker(source))
    assert [obj["metadata"]["name"] for obj in (config, vm, policy, service, monitor)] == [
        NAME,
        NAME,
        NAME,
        f"{NAME}-metrics",
        NAME,
    ]
    assert vm["spec"]["runStrategy"] == "Halted"
    assert vm["spec"]["template"]["spec"]["domain"]["resources"] == {
        "limits": {"cpu": "4", "memory": "16Gi"},
        "requests": {"cpu": "4", "memory": "16Gi"},
    }
    assert vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["storageClassName"] == ("piraeus-ssd-local-delete")
    assert vm["spec"]["dataVolumeTemplates"][0]["spec"]["storage"]["resources"]["requests"]["storage"] == ("80Gi")
    assert vm["spec"]["dataVolumeTemplates"][0]["metadata"]["name"] == f"{NAME}-work"
    assert (
        next(volume for volume in vm["spec"]["template"]["spec"]["volumes"] if "dataVolume" in volume)["dataVolume"][
            "name"
        ]
        == f"{NAME}-work"
    )
    assert (
        next(volume for volume in vm["spec"]["template"]["spec"]["volumes"] if "configMap" in volume)["configMap"][
            "name"
        ]
        == NAME
    )
    assert "--env MY_POOL=" + NAME in config["data"]["run-executor.sh"]
    executor = yaml.safe_load(config["data"]["executor.yaml"])["executor"]
    assert executor["millicpu"] == 4000
    assert executor["memory_bytes"] == 15 * 1024**3
    assert executor["max_concurrent_tasks"] == 1
    assert policy["spec"]["endpointSelector"]["matchLabels"] == {"app.kubernetes.io/part-of": NAME}
    assert service["spec"]["selector"] == {"app.kubernetes.io/part-of": NAME}
    assert monitor["spec"]["selector"]["matchLabels"] == {"app.kubernetes.io/part-of": NAME}


if __name__ == "__main__":
    pytest_bazel.main()
