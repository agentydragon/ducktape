import pytest_bazel
import yaml
from more_itertools import one

from util.bazel.runfiles import get_required_path


def test_mailbox_initialization_wiring() -> None:
    mailbox_path = get_required_path("_main/cluster/k8s/haku/mailbox/haku-mailbox.k8s.yaml")
    deployment = one(obj for obj in yaml.safe_load_all(mailbox_path.read_text()) if obj["kind"] == "Deployment")
    kustomization_path = get_required_path("_main/cluster/k8s/haku/mailbox/kustomization.yaml")
    kustomization = yaml.safe_load(kustomization_path.read_text())

    pod_spec = deployment["spec"]["template"]["spec"]

    config_generator = next(
        generator for generator in kustomization["configMapGenerator"] if "initialize.sh" in generator["files"]
    )
    assert any(volume.get("configMap", {}).get("name") == config_generator["name"] for volume in pod_spec["volumes"])


if __name__ == "__main__":
    pytest_bazel.main()
