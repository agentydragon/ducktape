"""Probe parser behavior on explicit LiteLLM config fixtures."""

from pathlib import Path

import yaml

from cluster.debug.litellm_probe.probe_models import ModelProbe, _load_model_probes


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
