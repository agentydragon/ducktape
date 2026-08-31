"""Contract between the deployed dispatch zones and generated worker configuration."""

from pathlib import Path

import pytest_bazel

from cluster.k8s.x.haku.dispatch.litellm.generate_workers_litellm import zai_zone_model_names
from haku.x.dispatch.config import load_zones


def test_deployed_zai_zone_models_match_generated_worker_models(k8s_dir: Path) -> None:
    zones = load_zones(k8s_dir / "x/haku/dispatch/dispatcher/zones.yaml")

    assert zones["zai"].models == set(zai_zone_model_names())


if __name__ == "__main__":
    pytest_bazel.main()
