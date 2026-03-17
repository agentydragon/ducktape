import pytest
import pytest_bazel
import yaml

from cluster.k8s.litellm.generate_litellm import generate
from util.bazel.runfiles import get_required_path


def test_litellm_yaml_matches_generator() -> None:
    committed_text = get_required_path("ducktape/cluster/k8s/litellm/proxy-config.yaml").read_text()
    committed = yaml.safe_load(committed_text)
    generated = yaml.safe_load(generate())
    if committed != generated:
        pytest.fail(
            "proxy-config.yaml is semantically out of sync with generate_litellm.py.\n"
            "Run: bazel run //cluster/k8s/litellm:generate_litellm_bin"
        )


if __name__ == "__main__":
    pytest_bazel.main()
