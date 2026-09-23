"""No Flux Kustomization sets both `wait: true` and `healthChecks`.

kustomize-controller ignores `spec.healthChecks` when `spec.wait` is true: it health-checks
every applied object instead, so such a list is dead config.
"""

from pathlib import Path

import pytest_bazel

from cluster.validation.flux import FluxKustomizationSpec, parse_flux_kustomizations


def _flux_kustomizations(k8s_dir: Path) -> dict[str, FluxKustomizationSpec]:
    """Every Flux Kustomization under cluster/k8s, rendered or hand-written, keyed by file and name."""
    return {
        f"{path.relative_to(k8s_dir)}: {name}": spec
        for path in sorted(k8s_dir.rglob("*.yaml"))
        if "kustomize.toolkit.fluxcd.io" in path.read_text()
        for name, spec in parse_flux_kustomizations(path).items()
    }


def test_wait_excludes_health_checks(k8s_dir: Path) -> None:
    kustomizations = _flux_kustomizations(k8s_dir)
    # Anti-vacuity: the scan reached the rendered node file, not just a handful of stragglers.
    assert sum(spec.wait for spec in kustomizations.values()) > 50
    violations = sorted(key for key, spec in kustomizations.items() if spec.wait and spec.health_checks)
    assert not violations, "wait: true ignores healthChecks; drop the list or set wait off:\n" + "\n".join(violations)


if __name__ == "__main__":
    pytest_bazel.main()
