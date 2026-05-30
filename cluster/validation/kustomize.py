"""Kustomize domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cluster.validation.k8s import K8sResource, parse_k8s_resources
from cluster.validation.tool_resolve import resolve_tool


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

    path: Path = Field(description="Absolute path to the kustomization.yaml file itself (injected by parser)")
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


async def run_kustomize_build(kustomization_path: Path) -> KustomizeBuildResult:
    """Run kustomize build and parse the output. Raises KustomizeBuildError on failure."""
    kustomize_bin = resolve_tool("kustomize", "multitool/tools/kustomize/kustomize")
    proc = await asyncio.create_subprocess_exec(
        kustomize_bin,
        "build",
        kustomization_path.parent,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise KustomizeBuildError(kustomization_path, stderr.decode())

    output = stdout.decode()
    resources = parse_k8s_resources(yaml.safe_load_all(output))

    return KustomizeBuildResult(kustomization_path=kustomization_path, resources=resources)
