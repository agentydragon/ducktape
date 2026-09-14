"""Synthesize the LiteLLM manifests with Python cdk8s."""

import argparse
from pathlib import Path

from cdk8s import App, Chart

from cluster.litellm_constructs import LiteLLMProxy, LiteLLMServiceMonitor, proxy_specs


def _config_maps() -> tuple[tuple[str, str, str, dict[str, object]], ...]:
    """Compatibility view used by the generator's parity tests."""
    return tuple((spec.name, spec.config.config_map_name, spec.namespace, spec.config.data) for spec in proxy_specs())


def generate_manifests(output_directory: Path) -> None:
    output_directory.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(output_directory))
    for spec in proxy_specs():
        chart = Chart(app, spec.name, disable_resource_name_hashes=True)
        LiteLLMProxy(chart, "proxy", spec)

    service_monitor_chart = Chart(app, "litellm-servicemonitor", disable_resource_name_hashes=True)
    LiteLLMServiceMonitor(service_monitor_chart, "monitoring")
    app.synth()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    generate_manifests(args.output_dir.resolve())


if __name__ == "__main__":
    main()
