"""Contract between the deployed dispatch zones and generated worker configuration."""

import pytest_bazel

from haku.x.dispatch.config import load_zones
from haku.x.dispatch.deploy.litellm.generate_workers_litellm import zai_zone_model_names
from util.bazel.runfiles import get_required_path

_ZONES = get_required_path("ducktape/haku/x/dispatch/deploy/dispatcher/zones.yaml")


def test_deployed_zai_zone_models_match_generated_worker_models() -> None:
    zones = load_zones(_ZONES)

    assert zones["zai"].models == set(zai_zone_model_names())


if __name__ == "__main__":
    pytest_bazel.main()
