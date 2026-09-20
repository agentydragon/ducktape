"""Contracts for source-artifact migrations."""

import shutil
import subprocess
from pathlib import Path

import pytest_bazel
import yaml
from more_itertools import one

from cluster.validation.tool_resolve import resolve_tool
from util.bazel.runfiles import get_required_path


def test_artifact_generators_preserve_render_inputs(tmp_path: Path) -> None:
    """Every generated artifact has exactly one live consumer, the directory it packages, and
    packaging that directory the way the generator does leaves `kustomize build` unchanged."""
    root = get_required_path("_main/cluster/k8s/kustomization.yaml").parent.resolve()
    kustomize = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")

    generators = [
        document
        for document in yaml.safe_load_all((root / "artifact-generators/artifact-generators.k8s.yaml").read_text())
        if document and document.get("kind") == "ArtifactGenerator"
    ]
    artifact_names = [artifact["name"] for generator in generators for artifact in generator["spec"]["artifacts"]]
    assert len(artifact_names) == len(set(artifact_names))
    generated_artifacts = {
        artifact["name"]: (generator, artifact)
        for generator in generators
        for artifact in generator["spec"]["artifacts"]
    }

    # A parked directory keeps its consumer declaration while nothing generates for it.
    consumers: dict[str, list[tuple[Path, dict]]] = {}
    flux_chart = root / "flux/kustomizations.k8s.yaml"
    for document in yaml.safe_load_all(flux_chart.read_text()):
        if not isinstance(document, dict) or document.get("kind") != "Kustomization":
            continue
        source = document["spec"].get("sourceRef", {})
        if source.get("kind") != "ExternalArtifact":
            continue
        relative = document["spec"]["path"].removeprefix("./")
        consumer_dir = (root.parent.parent / relative).resolve()
        if "parked" in consumer_dir.relative_to(root).parts:
            continue
        consumers.setdefault(source["name"], []).append((consumer_dir, document))
    assert set(consumers) == set(generated_artifacts)

    for artifact_name, (generator, artifact) in generated_artifacts.items():
        consumer_dir, consumer = one(consumers[artifact_name])
        relative = f"cluster/k8s/{consumer_dir.relative_to(root)}"
        aliases = {source["alias"] for source in generator["spec"]["sources"]}
        assert consumer["spec"]["sourceRef"] == {
            "kind": "ExternalArtifact",
            "name": artifact_name,
            "namespace": generator["metadata"]["namespace"],
        }
        assert consumer["spec"]["path"] == f"./{relative}"
        assert "revision" not in artifact  # Content-derived, not the monorepo revision.
        alias = artifact["originRevision"].removeprefix("@")
        assert alias in aliases
        primary_operation = one(
            operation for operation in artifact["copy"] if operation["to"] == f"@artifact/{relative}/"
        )
        assert primary_operation == {"from": f"@{alias}/{relative}/**", "to": f"@artifact/{relative}/"}
        packaged_root = tmp_path / artifact_name
        packaged = packaged_root / relative
        for operation in artifact["copy"]:
            operation_source_relative = operation["from"].removeprefix(f"@{alias}/").removesuffix("/**")
            operation_source = root / operation_source_relative.removeprefix("cluster/k8s/")
            operation_target = packaged_root / operation["to"].removeprefix("@artifact/")
            shutil.copytree(
                operation_source, operation_target, ignore=shutil.ignore_patterns(*operation.get("exclude", []))
            )
        assert not (packaged / "flux-kustomization.yaml").exists()
        original = subprocess.run([kustomize, "build", str(consumer_dir)], check=True, capture_output=True, text=True)
        rebuilt = subprocess.run([kustomize, "build", str(packaged)], check=True, capture_output=True, text=True)
        assert list(yaml.safe_load_all(rebuilt.stdout)) == list(yaml.safe_load_all(original.stdout))


if __name__ == "__main__":
    pytest_bazel.main()
