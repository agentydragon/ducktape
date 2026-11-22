"""Kustomize domain: models, parsing, and build execution."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from python.runfiles import Runfiles

from cluster.validation.k8s import K8sResource, load_k8s_resources

KUSTOMIZATION_FILE_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")


class _CamelCaseModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, alias_generator=to_camel)


class GeneratorInputs(_CamelCaseModel):
    files: list[str] = []
    envs: list[Path] = []


class PatchEntry(_CamelCaseModel):
    path: Path | None = None


class KustomizeFile(_CamelCaseModel):
    """Parsed kustomization.yaml — Pydantic coerces YAML string paths to Path objects."""

    path: Path = Field(
        description="Absolute path to the kustomization file itself (injected by parser), or where "
        "kustomize-controller writes the one it generates (`flux_generated_kustomization`)"
    )
    resources: list[Path] = []
    patches: list[PatchEntry] = []
    config_map_generator: list[GeneratorInputs] = []
    secret_generator: list[GeneratorInputs] = []
    configurations: list[Path] = []

    def _resolve(self, rel: Path) -> Path:
        return (self.path.parent / rel).resolve()

    @property
    def resolved_resources(self) -> list[Path]:
        return [self._resolve(r) for r in self.resources]

    @property
    def all_referenced_files(self) -> set[Path]:
        referenced = set(self.resolved_resources)
        referenced.update(self._resolve(patch.path) for patch in self.patches if patch.path)
        referenced.update(self._resolve(path) for path in self.configurations)
        for generator in (*self.config_map_generator, *self.secret_generator):
            # Both generator kinds accept "key=path" or just "path" in files.
            referenced.update(self._resolve(Path(file.split("=", 1)[-1])) for file in generator.files)
            referenced.update(self._resolve(path) for path in generator.envs)
        return referenced


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


def stage_artifact_copies(repo_root: Path, artifact_root: Path, copies: Sequence[tuple[str, str]]) -> None:
    """Materialize source-watcher `@repo/...` copies under a temporary artifact root.

    Directory copies already appear at their artifact paths in the source checkout. Supplemental
    files may not, so integration tests stage declared copies without modifying the checkout.
    This supports directory `/**` and exact-file copies; unsupported patterns fail rather than
    producing a partial artifact.
    """

    repo_root = repo_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)

    artifact_root = artifact_root.resolve()

    def resolve_copy_path(root: Path, uri: str, prefix: str, *, suffix: str = "") -> Path:
        if not uri.startswith(prefix):
            raise ValueError(f"artifact copy path must start with {prefix!r}: {uri!r}")
        path = uri.removeprefix(prefix).rstrip("/").removesuffix(suffix)
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise ValueError(f"artifact copy path must be a safe relative path: {uri!r}")
        if any(character in path for character in "*?[]{}"):
            raise ValueError(f"unsupported artifact copy pattern: {uri!r}")
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError(f"artifact copy path escapes {root}: {uri!r}")
        return resolved

    for source_uri, destination_uri in copies:
        is_directory = source_uri.rstrip("/").endswith("/**")
        source = resolve_copy_path(repo_root, source_uri, "@repo/", suffix="/**")
        destination = resolve_copy_path(artifact_root, destination_uri, "@artifact/")

        if is_directory:
            if not source.is_dir():
                raise FileNotFoundError(f"artifact directory source is missing: {source_uri!r}")
            shutil.copytree(source, destination, dirs_exist_ok=True)
        else:
            if not source.is_file():
                raise FileNotFoundError(f"artifact file source is missing: {source_uri!r}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)


def _kustomize_build_args(directory: Path, scratch: Path) -> list[str | Path]:
    if has_kustomization_file(directory):
        return [directory]
    kust = flux_generated_kustomization(directory)
    generated = {
        "apiVersion": "kustomize.config.k8s.io/v1beta1",
        "kind": "Kustomization",
        "resources": [os.path.relpath(r, scratch) for r in kust.resolved_resources],
    }
    (scratch / "kustomization.yaml").write_text(yaml.safe_dump(generated))
    return [scratch, "--load-restrictor", "LoadRestrictionsNone"]


async def run_kustomize_build(directory: Path) -> list[K8sResource]:
    """Build an explicit or Flux-generated kustomization and parse its resources."""
    kustomize_bin = Runfiles.CreateOrRaise().root() / "multitool/tools/kustomize/kustomize"
    with tempfile.TemporaryDirectory() as scratch:
        proc = await asyncio.create_subprocess_exec(
            kustomize_bin,
            "build",
            *_kustomize_build_args(directory, Path(scratch)),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(f"kustomize build failed for {directory}: {stderr.decode()}")

    return load_k8s_resources(stdout)
