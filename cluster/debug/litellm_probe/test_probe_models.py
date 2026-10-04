"""Probe input contracts: committed ConfigMap by default, raw config when supplied."""

from pathlib import Path

import pytest_bazel
import yaml

from cluster.debug.litellm_probe.probe_models import ModelProbe, _load_model_probes


def test_default_reads_committed_proxy_config() -> None:
    probes = _load_model_probes(None)
    assert probes
    assert len({probe.name for probe in probes}) == len(probes)
    assert any(probe.backend == "ollama" for probe in probes)
    assert any(probe.mode == "embedding" for probe in probes)


def test_explicit_config_is_still_raw_litellm_yaml(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": "example/chat",
                        "litellm_params": {"model": "openai/example"},
                        "model_info": {"mode": "responses"},
                    }
                ]
            }
        )
    )
    assert _load_model_probes(path) == [ModelProbe(name="example/chat", mode="responses", backend="openai")]


if __name__ == "__main__":
    pytest_bazel.main()
