"""Parallel kustomize validation script.

Validates all kustomizations quickly and quietly (unless errors occur).

Checks:
1. kustomize build succeeds for each kustomization
2. No duplicate external-secrets installations
3. CRD layering: HelmReleases are not mixed with CRD instances from external operators
4. Orphaned files: YAML files must be referenced by a kustomization.yaml
5. Dependency graph: No circular dependencies, required dependencies present
6. Flux build: Validates flux can build the complete kustomization tree

See AGENTS.md section "Flux Kustomization Layering" for CRD layering details.

Run via Bazel: bazel run //cluster/scripts:validate_kustomizations
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from bazel_util.workspace import get_build_workspace_directory
from cluster.scripts.runfiles_util import resolve_path

logger = logging.getLogger(__name__)

_KUSTOMIZE_BIN = resolve_path("multitool/tools/kustomize/kustomize")
_FLUX_BIN = resolve_path("multitool/tools/flux/flux")


# ============================================================================
# Pydantic Models - Single source of truth for parsed data
# ============================================================================


class DependsOn(BaseModel):
    """Flux Kustomization dependency reference."""

    model_config = ConfigDict(extra="ignore")

    name: str
    namespace: str | None = None


class KustomizeFile(BaseModel):
    """Parsed kustomization.yaml file."""

    path: Path
    resources: list[Path] = []  # Resolved absolute paths
    patches: list[Path] = []  # Resolved absolute paths (from patches: and patchesStrategicMerge:)


class FluxKustomization(BaseModel):
    """Parsed flux-kustomization.yaml Kustomization CR."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    name: str
    file_path: Path
    spec_path: str = Field(default="", alias="path")
    depends_on: list[DependsOn] = Field(default=[], alias="dependsOn")


class K8sMetadata(BaseModel):
    """Kubernetes resource metadata."""

    model_config = ConfigDict(extra="ignore")

    name: str = ""
    namespace: str = ""


class HelmChartSpec(BaseModel):
    """HelmRelease chart spec."""

    model_config = ConfigDict(extra="ignore")

    version: str | None = None


class HelmChart(BaseModel):
    """HelmRelease chart reference."""

    model_config = ConfigDict(extra="ignore")

    spec: HelmChartSpec = Field(default_factory=HelmChartSpec)


class HelmReleaseSpec(BaseModel):
    """HelmRelease spec."""

    model_config = ConfigDict(extra="ignore")

    chart: HelmChart = Field(default_factory=HelmChart)


class K8sResource(BaseModel):
    """Parsed Kubernetes resource from YAML."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: str
    api_version: str = Field(default="", alias="apiVersion")
    metadata: K8sMetadata = Field(default_factory=K8sMetadata)
    spec: HelmReleaseSpec | None = None

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def namespace(self) -> str:
        return self.metadata.namespace

    @property
    def chart_version(self) -> str | None:
        if self.kind != "HelmRelease" or not self.spec:
            return None
        return self.spec.chart.spec.version


class KustomizeBuildResult(BaseModel):
    """Result of running kustomize build on a directory."""

    kustomization_path: Path
    success: bool
    error: str = ""
    resources: list[K8sResource] = []


class ParsedCluster(BaseModel):
    """All parsed data from the cluster directory - parsed once, used everywhere."""

    kustomize_files: dict[Path, KustomizeFile] = {}
    flux_kustomizations: dict[str, FluxKustomization] = {}  # keyed by name
    all_yaml_files: set[Path] = set()
    source_resources: dict[Path, list[K8sResource]] = {}
    build_results: list[KustomizeBuildResult] = []


# ============================================================================
# Constants
# ============================================================================


# Map CRD kinds to their operator Kustomization names
CRD_TO_OPERATOR: dict[str, str] = {
    # external-secrets-operator
    "ExternalSecret": "external-secrets-operator",
    "ClusterExternalSecret": "external-secrets-operator",
    "SecretStore": "external-secrets-operator",
    "ClusterSecretStore": "external-secrets-operator",
    "Password": "external-secrets-operator",
    "Fake": "external-secrets-operator",
    "VaultDynamicSecret": "external-secrets-operator",
    # cert-manager
    "Certificate": "cert-manager",
    "CertificateRequest": "cert-manager",
    "Issuer": "cert-manager",
    "ClusterIssuer": "cert-manager",
    # kyverno
    "ClusterPolicy": "kyverno",
    "Policy": "kyverno",
    # metallb
    "IPAddressPool": "metallb",
    "L2Advertisement": "metallb",
    "BGPAdvertisement": "metallb",
    # vault-operator
    "Vault": "vault-operator",
    # tofu-controller (in core)
    "Terraform": "core",
    # powerdns-operator
    "ClusterZone": "powerdns-operator",
    "ClusterRRset": "powerdns-operator",
}

# These Kustomizations ARE the operators, so they don't need to depend on themselves
OPERATOR_KUSTOMIZATIONS = {
    "external-secrets-operator",
    "external-secrets",  # config kustomization
    "cert-manager",
    "cert-manager-config",
    "cert-manager-trust",
    "cert-manager-environment",
    "kyverno",
    "kyverno-policies",
    "metallb",
    "metallb-config",
    "vault-operator",
    "vault",
    "core",
    "powerdns-operator",
    "cluster-ca",  # Uses cert-manager CRDs but is part of cert-manager layer
}


# ============================================================================
# Parsing - Single pass over all files
# ============================================================================


def parse_k8s_resource(doc: dict) -> K8sResource | None:
    """Parse a single YAML document into a K8sResource."""
    if not doc or not isinstance(doc, dict):
        return None
    if not doc.get("kind"):
        return None
    return K8sResource.model_validate(doc)


def parse_kustomize_file(kust_file: Path) -> KustomizeFile | None:
    """Parse a kustomization.yaml file."""
    try:
        with kust_file.open() as f:
            doc = yaml.safe_load(f)
            if not doc:
                return None

            resources: list[Path] = []
            patches: list[Path] = []

            # Parse resources:
            for resource in doc.get("resources", []):
                resource_path = (kust_file.parent / resource).resolve()
                resources.append(resource_path)

            # Parse patches: (new format with path key)
            for patch in doc.get("patches", []):
                if isinstance(patch, dict) and "path" in patch:
                    patch_path = (kust_file.parent / patch["path"]).resolve()
                    patches.append(patch_path)

            # Parse patchesStrategicMerge: (legacy format)
            for patch in doc.get("patchesStrategicMerge", []):
                if isinstance(patch, str):
                    patch_path = (kust_file.parent / patch).resolve()
                    patches.append(patch_path)

            return KustomizeFile(path=kust_file, resources=resources, patches=patches)

    except (yaml.YAMLError, OSError) as e:
        logger.debug("Failed to parse kustomization %s: %s", kust_file, e)
        return None


def parse_flux_kustomization(flux_file: Path) -> list[FluxKustomization]:
    """Parse a flux-kustomization.yaml file (may contain multiple documents)."""
    results = []
    try:
        with flux_file.open() as f:
            docs = list(yaml.safe_load_all(f))
            for doc in docs:
                if not doc:
                    continue
                if doc.get("kind") != "Kustomization":
                    continue
                if not doc.get("apiVersion", "").startswith("kustomize.toolkit.fluxcd.io"):
                    continue

                metadata = doc.get("metadata", {}) or {}
                name = metadata.get("name", "")
                if not name:
                    continue

                spec = doc.get("spec", {}) or {}
                results.append(FluxKustomization.model_validate({"name": name, "file_path": flux_file, **spec}))

    except (yaml.YAMLError, OSError) as e:
        logger.debug("Failed to parse flux kustomization %s: %s", flux_file, e)

    return results


def parse_yaml_file(yaml_file: Path) -> list[K8sResource]:
    """Parse a YAML file and extract all K8s resources."""
    resources = []
    try:
        with yaml_file.open() as f:
            docs = list(yaml.safe_load_all(f))
            for doc in docs:
                resource = parse_k8s_resource(doc)
                if resource:
                    resources.append(resource)
    except (yaml.YAMLError, OSError) as e:
        # YAML files might contain Go templating - warn but continue
        logger.debug("Failed to parse %s: %s", yaml_file, e)

    return resources


def parse_cluster(k8s_dir: Path) -> ParsedCluster:
    """Parse all files in the cluster directory once."""
    cluster = ParsedCluster()

    for yaml_file in k8s_dir.rglob("*.yaml"):
        # Skip flux-system (auto-generated)
        if "flux-system" in yaml_file.parts:
            continue

        # Skip charts directory (Helm templates)
        if "charts" in yaml_file.parts:
            continue

        cluster.all_yaml_files.add(yaml_file.resolve())

        # Parse kustomization.yaml files
        if yaml_file.name == "kustomization.yaml":
            kust = parse_kustomize_file(yaml_file)
            if kust:
                cluster.kustomize_files[yaml_file] = kust

        # Parse flux-kustomization.yaml files
        elif yaml_file.name == "flux-kustomization.yaml":
            for flux_kust in parse_flux_kustomization(yaml_file):
                cluster.flux_kustomizations[flux_kust.name] = flux_kust

        # Parse other YAML files for K8s resources
        else:
            resources = parse_yaml_file(yaml_file)
            if resources:
                cluster.source_resources[yaml_file] = resources

    return cluster


# ============================================================================
# Kustomize Build (async)
# ============================================================================


async def run_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult:
    """Run kustomize build and parse the output."""
    try:
        proc = await asyncio.create_subprocess_exec(
            _KUSTOMIZE_BIN,
            "build",
            kustomization_path.parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            return KustomizeBuildResult(kustomization_path=kustomization_path, success=False, error=stderr.decode())

        # Parse the output once
        resources = []
        output = stdout.decode()
        for doc in yaml.safe_load_all(output):
            resource = parse_k8s_resource(doc)
            if resource:
                resources.append(resource)

        return KustomizeBuildResult(kustomization_path=kustomization_path, success=True, resources=resources)

    except Exception as e:
        return KustomizeBuildResult(kustomization_path=kustomization_path, success=False, error=str(e))


# ============================================================================
# Validation Functions - Use parsed data, no re-parsing
# ============================================================================


def find_orphaned_files(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Find YAML files not referenced by any kustomization."""
    errors = []

    # Build set of all referenced files
    referenced: set[Path] = set()
    for kust in cluster.kustomize_files.values():
        referenced.update(kust.resources)
        referenced.update(kust.patches)
        # If resource is a directory, also mark its kustomization.yaml
        for resource in kust.resources:
            if resource.is_dir():
                referenced.add(resource / "kustomization.yaml")

    # Check each YAML file
    for yaml_file in cluster.all_yaml_files:
        # Skip kustomization.yaml files themselves
        if yaml_file.name == "kustomization.yaml":
            continue

        if yaml_file not in referenced:
            try:
                relative = yaml_file.relative_to(k8s_dir)
                errors.append(f"Orphaned file not referenced by any kustomization: {relative}")
            except ValueError:
                errors.append(f"Orphaned file: {yaml_file}")

    return errors


def check_duplicate_external_secrets(build_results: list[KustomizeBuildResult]) -> list[str]:
    """Check for duplicate external-secrets HelmRelease installations."""
    errors = []
    deployments: dict[str, list[str]] = defaultdict(list)

    for result in build_results:
        if not result.success:
            continue
        for resource in result.resources:
            if resource.kind == "HelmRelease" and resource.name == "external-secrets":
                key = f"{resource.namespace}/{resource.chart_version or 'unknown'}"
                deployments[key].append(str(result.kustomization_path.parent))

    if len(deployments) > 1:
        errors.append("Multiple external-secrets HelmRelease found:")
        for deployment, paths in deployments.items():
            errors.append(f"  {deployment}: {', '.join(paths)}")
        errors.append("There should be exactly ONE external-secrets installation.")
    elif len(deployments) == 0:
        errors.append("No external-secrets HelmRelease found. At least one is required.")

    return errors


def check_crd_layering(result: KustomizeBuildResult) -> list[str]:
    """Check if a kustomization mixes HelmReleases with CRD instances."""
    if not result.success:
        return []

    kust_name = result.kustomization_path.parent.name

    # Skip operator kustomizations
    if any(part in OPERATOR_KUSTOMIZATIONS for part in result.kustomization_path.parent.parts):
        return []

    # Skip overlay directories
    if "overlays" in result.kustomization_path.parts:
        return []

    has_helmrelease = False
    crd_instances: list[tuple[str, str]] = []

    for resource in result.resources:
        if resource.kind == "HelmRelease":
            has_helmrelease = True
        if resource.kind in CRD_TO_OPERATOR:
            crd_instances.append((resource.kind, CRD_TO_OPERATOR[resource.kind]))

    if has_helmrelease and crd_instances:
        unique_crds = sorted({f"{k} (needs {op})" for k, op in crd_instances})
        return [
            f"CRD layering violation: Mixes HelmRelease with CRD instances: {', '.join(unique_crds)}. "
            f"Split CRD instances into a separate '{kust_name}-secrets/' Kustomization. "
            f"See AGENTS.md 'Flux Kustomization Layering'."
        ]

    return []


def build_dependency_graph(flux_kustomizations: dict[str, FluxKustomization]) -> dict[str, list[str]]:
    """Build dependency graph from flux kustomizations."""
    graph: dict[str, list[str]] = defaultdict(list)
    for name, kust in flux_kustomizations.items():
        for dep in kust.depends_on:
            graph[dep.name].append(name)
    return dict(graph)


def find_cycles(graph: dict[str, list[str]], all_nodes: set[str]) -> list[list[str]]:
    """Find cycles in dependency graph using DFS."""
    unvisited, in_progress, done = 0, 1, 2
    color = dict.fromkeys(all_nodes, unvisited)
    cycles = []

    def dfs(node: str, path: list[str]) -> None:
        if color[node] == in_progress:
            cycle_start = path.index(node)
            cycles.append([*path[cycle_start:], node])
            return
        if color[node] == done:
            return

        color[node] = in_progress
        path.append(node)
        for neighbor in graph.get(node, []):
            dfs(neighbor, path)
        path.pop()
        color[node] = done

    for node in all_nodes:
        if color[node] == unvisited:
            dfs(node, [])

    return cycles


def check_required_dependencies(flux_kustomizations: dict[str, FluxKustomization]) -> list[str]:
    """Check that critical dependencies are correctly set up."""
    errors = []

    dependency_rules = {
        "external-secrets-config": {
            "must_come_before": ["authentik", "gitea", "harbor", "powerdns", "matrix"],
            "reason": "Applications need external-secrets ClusterSecretStore to sync secrets from Vault",
        },
        "cert-manager": {
            "must_come_before": ["ingress-nginx", "authentik", "gitea", "harbor"],
            "reason": "TLS certificates required for ingress and applications",
        },
        "ingress-nginx": {
            "must_come_before": ["authentik", "gitea", "harbor", "matrix"],
            "reason": "Applications need ingress controller for external access",
        },
        "vault": {
            "must_come_before": ["external-secrets-operator", "external-secrets-config"],
            "reason": "Vault must be ready before external-secrets can connect",
        },
        "metallb-config": {
            "must_come_before": ["ingress-nginx"],
            "reason": "Load balancer needed for ingress controller",
        },
    }

    # Build dependency lookup
    depends_on_map: dict[str, list[str]] = {}
    for name, kust in flux_kustomizations.items():
        depends_on_map[name] = [dep.name for dep in kust.depends_on]

    def has_dependency_path(from_kust: str, to_kust: str, visited: set[str] | None = None) -> bool:
        if visited is None:
            visited = set()
        if to_kust in visited:
            return False
        if from_kust == to_kust:
            return True
        visited.add(to_kust)
        return any(has_dependency_path(from_kust, dep, visited) for dep in depends_on_map.get(to_kust, []))

    for prereq, rule in dependency_rules.items():
        if prereq not in flux_kustomizations:
            continue
        for dependent in rule["must_come_before"]:
            if dependent not in flux_kustomizations:
                continue
            if prereq not in depends_on_map.get(dependent, []):
                has_transitive = any(has_dependency_path(prereq, dep) for dep in depends_on_map.get(dependent, []))
                if not has_transitive:
                    errors.append(f"{dependent} should depend on {prereq} ({rule['reason']})")

    return errors


def validate_external_secrets_dependencies(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate external-secrets specific dependency patterns."""
    errors = []
    services_with_external_secrets: set[str] = set()

    # Find services using ExternalSecret resources
    for file_path, resources in cluster.source_resources.items():
        for resource in resources:
            if resource.kind == "ExternalSecret" and resource.api_version.startswith("external-secrets.io"):
                try:
                    relative = file_path.relative_to(k8s_dir)
                    service_name = relative.parts[0] if relative.parts else None
                    if service_name:
                        services_with_external_secrets.add(service_name)
                except ValueError:
                    pass

    # Check dependencies
    for service in services_with_external_secrets:
        if service in cluster.flux_kustomizations:
            deps = [dep.name for dep in cluster.flux_kustomizations[service].depends_on]
            if "external-secrets-config" not in deps:
                errors.append(f"{service} uses ExternalSecret resources but doesn't depend on external-secrets-config")

    return errors


def validate_dependencies(cluster: ParsedCluster, k8s_dir: Path) -> list[str]:
    """Validate GitOps dependency graph."""
    errors = []

    if not cluster.flux_kustomizations:
        errors.append("No Flux kustomizations found")
        return errors

    graph = build_dependency_graph(cluster.flux_kustomizations)
    all_nodes = (
        set(cluster.flux_kustomizations.keys()) | set().union(*graph.values())
        if graph
        else set(cluster.flux_kustomizations.keys())
    )

    cycles = find_cycles(graph, all_nodes)
    for cycle in cycles:
        errors.append(f"Circular dependency: {' → '.join(cycle)}")

    errors.extend(check_required_dependencies(cluster.flux_kustomizations))
    errors.extend(validate_external_secrets_dependencies(cluster, k8s_dir))

    return errors


def run_flux_build(k8s_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run flux build and return the result."""
    kustomization_file = k8s_dir / "flux-system" / "gotk-sync.yaml"

    if not kustomization_file.exists():
        raise FileNotFoundError(f"gotk-sync.yaml not found at {kustomization_file}")

    return subprocess.run(
        [
            _FLUX_BIN,
            "build",
            "kustomization",
            "flux-system",
            "--path",
            k8s_dir,
            "--kustomization-file",
            kustomization_file,
            "--dry-run",
            "--verbose",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def validate_flux_build(k8s_dir: Path) -> list[str]:
    """Validate flux build."""
    try:
        result = run_flux_build(k8s_dir)
    except FileNotFoundError as e:
        return [str(e)]

    if result.returncode != 0:
        return [f"flux build failed:\nk8s_dir: {k8s_dir}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"]

    if not result.stdout.strip():
        return [f"flux build returned empty output:\nk8s_dir: {k8s_dir}\nstderr: {result.stderr.strip() or 'none'}"]

    # Analyze output
    errors = []
    resource_counts: Counter[str] = Counter()

    for doc in yaml.safe_load_all(result.stdout):
        resource = parse_k8s_resource(doc)
        if resource:
            resource_counts[resource.kind] += 1

    if resource_counts.get("Kustomization", 0) == 0:
        errors.append("No Flux Kustomization resources found in flux build output")
    if resource_counts.get("GitRepository", 0) == 0:
        errors.append("No GitRepository resource found in flux build output")

    return errors


# ============================================================================
# Main
# ============================================================================


async def main() -> int:
    parser = argparse.ArgumentParser(description="Validate kustomizations in parallel")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show successful validations")
    parser.add_argument("--root", type=Path, help="Root directory to search for kustomizations")
    parser.add_argument(
        "--format", choices=["human", "json"], default="human", help="Output format (human or json for Terraform)"
    )
    parser.add_argument(
        "--skip-flux-build",
        action="store_true",
        help="Skip flux build validation (useful when flux-system not bootstrapped)",
    )
    parser.add_argument("--skip-dependencies", action="store_true", help="Skip dependency graph validation")
    args = parser.parse_args()

    root = args.root or (get_build_workspace_directory() / "cluster" / "k8s")

    # Parse all files once
    cluster = parse_cluster(root)

    # Find kustomization.yaml files for build validation
    kustomization_files = list(cluster.kustomize_files)

    if not kustomization_files:
        print(f"No kustomizations found in {root}")
        return 0

    # Run kustomize build in parallel
    tasks = [run_kustomize_build(k) for k in kustomization_files]
    cluster.build_results = await asyncio.gather(*tasks)

    # Collect errors
    kust_errors: list[tuple[Path, str]] = []
    global_errors: list[str] = []

    successful = [r for r in cluster.build_results if r.success]
    for result in cluster.build_results:
        if not result.success:
            kust_errors.append((result.kustomization_path, result.error))

    # Check duplicate external-secrets
    global_errors.extend(check_duplicate_external_secrets(cluster.build_results))

    # Check CRD layering
    for result in cluster.build_results:
        for error in check_crd_layering(result):
            kust_errors.append((result.kustomization_path, error))

    # Check orphaned files
    global_errors.extend(find_orphaned_files(cluster, root))

    # Validate dependencies
    if not args.skip_dependencies:
        global_errors.extend(validate_dependencies(cluster, root))

    # Validate flux build
    if not args.skip_flux_build:
        global_errors.extend(validate_flux_build(root))

    has_errors = bool(kust_errors or global_errors)

    # Output results
    if args.format == "json":
        if has_errors:
            error_details = [{"path": str(k.parent), "error": error.strip()} for k, error in kust_errors]
            error_details.extend([{"path": "", "error": error.strip()} for error in global_errors])
            result_json = {"error": f"Validation failed with {len(error_details)} errors", "details": error_details}
            print(json.dumps(result_json), file=sys.stderr)
            return 1
        result_json = {"status": "passed", "validated_count": str(len(successful))}
        print(json.dumps(result_json))
        return 0

    # Human-readable output
    if args.verbose and successful:
        print(f"✅ Successfully validated {len(successful)} kustomizations:")
        for r in successful:
            print(f"  {r.kustomization_path.parent}")

    if has_errors:
        print("❌ Validation failed:")
        for kustomization, error in kust_errors:
            print(f"  {kustomization.parent}:")
            print(f"    {error.strip()}")
        for error in global_errors:
            print(f"  {error.strip()}")
        return 1

    if not args.verbose:
        print(f"✅ All {len(successful)} kustomizations valid, no dependency/layering issues")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
