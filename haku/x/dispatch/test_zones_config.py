"""Parity: the deployed zones.yaml must agree with the generated
workers-LiteLLM config — the zone allowlists exactly the zai models it serves."""

import pytest_bazel

from cluster.k8s.x.haku.dispatch.litellm.generate_workers_litellm import zai_zone_model_names
from haku.x.dispatch.config import load_zones
from util.bazel.runfiles import get_required_path

_ZONES = load_zones(get_required_path("ducktape/cluster/k8s/x/haku/dispatch/dispatcher/zones.yaml"))


def test_zai_zone_models_match_workers_litellm():
    assert _ZONES["zai"].models == set(zai_zone_model_names())


if __name__ == "__main__":
    pytest_bazel.main()
