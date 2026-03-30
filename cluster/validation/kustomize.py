"""Kustomize domain: models and parsing."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cluster.validation.k8s import K8sResource


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


def parse_kustomize_file(kust_file: Path) -> KustomizeFile | None:
    """Parse a kustomization.yaml file. Returns None only for empty files."""
    with kust_file.open() as f:
        doc = yaml.safe_load(f)
        if not doc:
            return None

    if "patchesStrategicMerge" in doc:
        raise ValueError(
            f"{kust_file}: uses deprecated 'patchesStrategicMerge'. "
            "Convert to 'patches' format (list of {{path: ...}} objects)."
        )

    return KustomizeFile.model_validate(doc | {"path": kust_file})
