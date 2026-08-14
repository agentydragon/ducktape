import pytest
import pytest_bazel
import yaml

from cluster.k8s.x.haku.dispatch.litellm.generate_workers_litellm import generate, zai_zone_model_names
from util.bazel.runfiles import get_required_path


def test_workers_config_matches_generator() -> None:
    committed = yaml.safe_load(
        get_required_path("ducktape/cluster/k8s/x/haku/dispatch/litellm/workers-litellm-config.yaml").read_text()
    )
    if committed != yaml.safe_load(generate()):
        pytest.fail(
            "workers-litellm-config.yaml is semantically out of sync with its generator.\n"
            "Run: bazel run //cluster/k8s/x/haku/dispatch/litellm:generate_workers_litellm_bin"
        )


def test_archived_zone_roster_is_nonempty() -> None:
    """The archived generator still has an explicit historical model roster."""
    assert zai_zone_model_names()


if __name__ == "__main__":
    pytest_bazel.main()
