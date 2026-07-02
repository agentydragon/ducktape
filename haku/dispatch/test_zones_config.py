"""Parity: the deployed zones.yaml must agree with the rest of the dispatch
plane — the workers-LiteLLM serves exactly the zai models the zone allowlists,
and the namespace matches the stamped perimeter."""

import pytest_bazel

from cluster.k8s.haku.dispatch.litellm.generate_workers_litellm import zai_zone_model_names
from haku.dispatch.config import load_zones
from util.bazel.runfiles import get_required_path

_ZONES = load_zones(get_required_path("ducktape/cluster/k8s/haku/dispatch/dispatcher/zones.yaml"))


def test_zai_zone_wiring():
    assert _ZONES["zai"].namespace == "haku-sandbox-zai"
    assert _ZONES["zai"].models == set(zai_zone_model_names())


def test_no_unknown_zones():
    # Each zone here needs a perimeter namespace + zone key + workers-LiteLLM
    # model group; adding one is a reviewed, multi-file change (build order
    # step 5 for oai) — a surprise entry means drift.
    assert set(_ZONES) == {"zai"}


if __name__ == "__main__":
    pytest_bazel.main()
