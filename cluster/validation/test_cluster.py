"""Tests for cluster parsing."""

import textwrap
from pathlib import Path

import pytest_bazel

from cluster.validation.cluster import parse_cluster


def test_artifact_generator_artifacts_are_source_ref_targets(tmp_path: Path) -> None:
    """An ArtifactGenerator's artifacts index as sourceRef targets in its own namespace: the
    ExternalArtifacts source-watcher creates from it are never in Git, so a consumer's
    ExternalArtifact sourceRef must resolve through the generator."""
    generator_dir = tmp_path / "kyverno" / "app-artifact"
    generator_dir.mkdir(parents=True)
    (generator_dir / "artifactgenerator.yaml").write_text(
        textwrap.dedent(
            """\
            apiVersion: source.extensions.fluxcd.io/v1beta1
            kind: ArtifactGenerator
            metadata:
              name: kyverno
              namespace: ducktape-flux
            spec:
              sources:
                - alias: repo
                  kind: GitRepository
                  name: ducktape
                  namespace: ducktape-flux
              artifacts:
                - name: kyverno
                  originRevision: "@repo"
                  copy:
                    - from: "@repo/cluster/k8s/kyverno/app/**"
                      to: "@artifact/cluster/k8s/kyverno/app/"
            """
        )
    )
    parsed = parse_cluster(tmp_path)
    assert ("ExternalArtifact", "ducktape-flux", "kyverno") in parsed.flux_sources
    assert parsed.artifact_paths[("ducktape-flux", "kyverno")] == {"cluster/k8s/kyverno/app"}


if __name__ == "__main__":
    pytest_bazel.main()
