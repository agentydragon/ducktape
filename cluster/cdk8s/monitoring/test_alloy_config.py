"""Validate the complete new log pipelines with Alloy's own component schemas."""

from contextlib import closing
from pathlib import Path

import docker
from python.runfiles import Runfiles

from devinfra.oci.oci import load_oci_image
from third_party.containers import alloy_config


def test_log_collection_configs(tmp_path: Path, runfiles: Runfiles) -> None:
    root = runfiles.root() / "ducktape/cluster/cdk8s/monitoring"
    image = load_oci_image(alloy_config.LAYOUT_RLOCATION)
    configs = (("node.alloy", "pod-logs.alloy"), ("journal.alloy",))
    with closing(docker.from_env()) as client:
        for files in configs:
            config = tmp_path / "config.alloy"
            config.write_text("\n".join((root / name).read_text() for name in files))
            client.containers.run(
                image,
                ["validate", "/etc/alloy/config.alloy"],
                volumes={str(config): {"bind": "/etc/alloy/config.alloy", "mode": "ro"}},
                environment={
                    "NODE_NAME": "test-node",
                    "MIMIR_PUSH_URL": "http://mimir.example.invalid/api/v1/push",
                    "LOKI_PUSH_URL": "http://loki.example.invalid/loki/api/v1/push",
                },
                network_disabled=True,
                remove=True,
            )
