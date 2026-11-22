"""Flux must supply a namespace for generated ConfigMaps and Secrets.

A missing namespace prevented the public-coder Deployment from reconciling in July 2026.
Check hand-written Kustomizations directly applied by Flux. Base fragments inherit their
owner's namespace, and cdk8s checks the Kustomizations it generates.
"""

from pathlib import Path

import pytest_bazel
import yaml

from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT, MANIFEST_ROOTS
from cluster.validation.flux import parse_flux_kustomizations

GENERATOR_FIELDS = ("configMapGenerator", "secretGenerator")


def test_generators_declare_a_namespace(repo_root: Path) -> None:
    offenders: list[str] = []
    flux = repo_root / HAND_WRITTEN_ROOT / "flux/kustomizations.k8s.yaml"
    for owner, spec in parse_flux_kustomizations(flux).items():
        path = Path(spec.path)
        if not any(path.is_relative_to(root) for root in MANIFEST_ROOTS) or path.is_relative_to(GENERATED_ROOT):
            continue
        directory = repo_root / path
        assert directory.is_dir(), f"{owner}: {path} missing from cluster validation runfiles"
        kust = directory / "kustomization.yaml"
        if spec.target_namespace or not kust.is_file():
            continue
        doc = yaml.safe_load(kust.read_text())
        if not isinstance(doc, dict) or doc.get("namespace"):
            continue
        # A generator used purely as a `replacements` source is never applied, so
        # it needs no namespace -- e.g. seaweedfs/cluster's filer.toml, which is
        # spliced into a Helm value and confirmed absent from the live cluster.
        replacement_sources = {r.get("source", {}).get("name") for r in doc.get("replacements") or []}
        for field in GENERATOR_FIELDS:
            for entry in doc.get(field) or []:
                if entry.get("name") in replacement_sources:
                    continue
                if not entry.get("namespace"):
                    offenders.append(
                        f"{owner} ({kust.relative_to(repo_root)}): {field} entry "
                        f"{entry.get('name', '<unnamed>')!r} has no `namespace:`"
                    )

    assert not offenders, (
        "Generated ConfigMaps/Secrets have no namespace of their own, so the apply fails with "
        '"namespace not specified" and Flux applies nothing from that directory. '
        "Add `namespace: <ns>` to the kustomization:\n" + "\n".join(f"  {o}" for o in offenders)
    )


if __name__ == "__main__":
    pytest_bazel.main()
