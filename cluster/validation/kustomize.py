"""Kustomize domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cluster.validation.k8s import K8sResource, parse_k8s_resources
from cluster.validation.tool_resolve import resolve_tool

KUSTOMIZATION_FILE_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")


class _CamelCaseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)


class GeneratorOptions(_CamelCaseModel):
    disable_name_suffix_hash: bool = False


class ConfigMapGeneratorEntry(_CamelCaseModel):
    name: str
    namespace: str = ""
    files: list[Path] = []
    options: GeneratorOptions = Field(default_factory=GeneratorOptions)


class PatchEntry(_CamelCaseModel):
    path: Path | None = None


class KustomizeFile(_CamelCaseModel):
    """Parsed kustomization.yaml — Pydantic coerces YAML string paths to Path objects."""

    path: Path = Field(
        description="Absolute path to the kustomization file itself (injected by parser), or where "
        "kustomize-controller writes the one it generates (`flux_generated_kustomization`)"
    )
    namespace: str = ""
    resources: list[Path] = []
    patches: list[PatchEntry] = []
    config_map_generator: list[ConfigMapGeneratorEntry] = []
    generator_options: GeneratorOptions = Field(default_factory=GeneratorOptions)

    def _resolve(self, rel: Path) -> Path:
        return (self.path.parent / rel).resolve()

    @property
    def resolved_resources(self) -> list[Path]:
        return [self._resolve(r) for r in self.resources]

    @property
    def resolved_patches(self) -> list[Path]:
        return [self._resolve(p.path) for p in self.patches if p.path]

    @staticmethod
    def _strip_configmap_key(p: Path) -> Path:
        """Handle `key=filename` format in configMapGenerator files entries."""
        name = str(p)
        if "=" in name:
            return Path(name.split("=", 1)[1])
        return p

    @property
    def resolved_generator_files(self) -> list[Path]:
        return [self._resolve(self._strip_configmap_key(f)) for entry in self.config_map_generator for f in entry.files]

    @property
    def all_referenced_files(self) -> set[Path]:
        result: set[Path] = set()
        result.update(self.resolved_resources)
        result.update(self.resolved_patches)
        result.update(self.resolved_generator_files)
        return result


class KustomizeBuildResult(BaseModel):
    """Successful kustomize build output for a single kustomization directory."""

    kustomization_path: Path
    resources: list[K8sResource] = []


def has_kustomization_file(directory: Path) -> bool:
    return any((directory / name).is_file() for name in KUSTOMIZATION_FILE_NAMES)


def flux_generated_kustomization(directory: Path) -> KustomizeFile:
    """The kustomization kustomize-controller generates for a Flux `spec.path` that has none
    (fluxcd/pkg/kustomize `scanManifests`): every `.yaml`/`.yml` file under `directory`,
    recursively, except that a subdirectory holding a kustomization file is listed whole."""
    resources: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(directory):
        here = Path(dirpath)
        dirnames.sort()
        for name in list(dirnames):
            if has_kustomization_file(here / name):
                resources.append((here / name).relative_to(directory))
                dirnames.remove(name)
        resources += [(here / f).relative_to(directory) for f in sorted(filenames) if f.endswith((".yaml", ".yml"))]
    return KustomizeFile(path=directory / "kustomization.yaml", resources=resources)


def parse_kustomize_file(kust_file: Path) -> KustomizeFile:
    """Parse a kustomization.yaml file."""
    with kust_file.open() as f:
        doc = yaml.safe_load(f)
        if not doc:
            raise ValueError(f"{kust_file}: empty kustomization.yaml")

    if "patchesStrategicMerge" in doc:
        raise ValueError(
            f"{kust_file}: uses deprecated 'patchesStrategicMerge'. "
            "Convert to 'patches' format (list of {{path: ...}} objects)."
        )

    return KustomizeFile.model_validate(doc | {"path": kust_file})


class KustomizeBuildError(Exception):
    """Raised when kustomize build fails."""

    def __init__(self, kustomization_path: Path, error: str) -> None:
        self.kustomization_path = kustomization_path
        super().__init__(f"kustomize build failed for {kustomization_path.parent}: {error}")


def _kustomize_build_args(kust: KustomizeFile, scratch: Path) -> list[str | Path]:
    """`kustomize build` arguments for `kust`. A generated one (no file at its path) is built
    the way kustomize-controller builds it: written out, here into `scratch`, and built
    without load restrictions."""
    if kust.path.is_file():
        return [kust.path.parent]
    generated = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [os.path.relpath(r, scratch) for r in kust.resolved_resources],
    }
    (scratch / "kustomization.yaml").write_text(yaml.safe_dump(generated))
    return [scratch, "--load-restrictor", "LoadRestrictionsNone"]


async def run_kustomize_build(kust: KustomizeFile) -> KustomizeBuildResult:
    """Run kustomize build and parse the output. Raises KustomizeBuildError on failure."""
    kustomize_bin = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")
    with tempfile.TemporaryDirectory() as scratch:
        proc = await asyncio.create_subprocess_exec(
            kustomize_bin,
            "build",
            *_kustomize_build_args(kust, Path(scratch)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise KustomizeBuildError(kust.path, stderr.decode())

    output = stdout.decode()
    resources = parse_k8s_resources(yaml.safe_load_all(output))

    return KustomizeBuildResult(kustomization_path=kust.path, resources=resources)
