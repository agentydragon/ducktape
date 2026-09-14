"""Generate a small cdk8s manifest set as a Bazel-local smoke test."""

import argparse
from pathlib import Path

from cdk8s import ApiObject, App, Chart


def generate_manifests(output_directory: Path) -> None:
    """Synthesize ordinary Kubernetes YAML into output_directory."""
    output_directory.mkdir(parents=True, exist_ok=True)

    app = App(outdir=str(output_directory))
    chart = Chart(app, "cdk8s-bazel-smoke", disable_resource_name_hashes=True)
    ApiObject(
        chart,
        "generated-config",
        api_version="v1",
        kind="ConfigMap",
        metadata={
            "name": "cdk8s-bazel-smoke",
            "labels": {"app.kubernetes.io/name": "cdk8s-bazel-smoke", "app.kubernetes.io/managed-by": "cdk8s"},
            "annotations": {
                "ducktape.dev/generated": "by cdk8s under Bazel",
                "ducktape.dev/delivery": "Flux can consume this ordinary Kubernetes YAML",
            },
        },
    )
    app.synth()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    generate_manifests(args.output_dir.resolve())


if __name__ == "__main__":
    main()
