"""Identify and reuse the pinned BuildBuddy remote-runner image when its inputs match."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PLATFORM = "linux/amd64"
INPUTS_LABEL = "works.allegedly.ducktape.runner-inputs"
TOOLS_ATTR = ".#packages.x86_64-linux.buildbuddy-remote-runner-tools.outPath"
RECIPE_FILES = (
    "devinfra/buildbuddy_remote_runner/Dockerfile",
    ".dockerignore",
    ".github/actions/ghcr-build-push/action.yml",
    ".github/workflows/container-images.yml",
    "nix/attic-pubkeys.json",
    "devinfra/ci/runner_image.py",
)
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclasses.dataclass(frozen=True)
class Inputs:
    """The evaluated tools closure and every explicit runner-image recipe input."""

    tools_path: str
    base_ref: str
    recipe_digests: tuple[tuple[str, str], ...]
    platform: str = PLATFORM

    @property
    def identity(self) -> str:
        payload = {
            "base_ref": self.base_ref,
            "platform": self.platform,
            "recipe_digests": dict(self.recipe_digests),
            "tools_path": self.tools_path,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(canonical).hexdigest()


def evaluate_tools_path(root: Path) -> str:
    result = subprocess.run(
        ["nix", "eval", "--raw", TOOLS_ATTR], cwd=root, check=True, stdout=subprocess.PIPE, text=True
    )
    tools_path = result.stdout.strip()
    if not tools_path:
        raise ValueError("Nix returned an empty runner-tools outPath")
    return tools_path


def _base_ref(root: Path) -> str:
    pins = json.loads((root / "devinfra/image_pins.json").read_text(encoding="utf-8"))
    pin = pins["rbe_container"]
    image, digest = pin["image"], pin["digest"]
    if not isinstance(image, str) or not image or "@" in image:
        raise ValueError(f"invalid RBE base image name: {image!r}")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise ValueError(f"invalid RBE base image digest: {digest!r}")
    return f"{image}@{digest}"


def compute_inputs(root: Path, tools_path: str) -> Inputs:
    """Hash exactly the evaluated closure, base ref, platform, and recipe files."""
    root = root.resolve()
    digests = tuple(
        (relative_path, hashlib.sha256((root / relative_path).read_bytes()).hexdigest())
        for relative_path in RECIPE_FILES
    )
    return Inputs(tools_path=tools_path, base_ref=_base_ref(root), recipe_digests=digests)


def load_inputs(root: Path) -> Inputs:
    return compute_inputs(root, evaluate_tools_path(root))


def _pinned_runner_ref(root: Path) -> str:
    config = json.loads((root / "devinfra/bbr.json").read_text(encoding="utf-8"))
    image_ref = config["container_image"]
    if not isinstance(image_ref, str) or "@" not in image_ref:
        raise ValueError(f"runner container_image must be pinned by digest: {image_ref!r}")
    image, digest = image_ref.rsplit("@", 1)
    if not image or not _SHA256.fullmatch(digest):
        raise ValueError(f"invalid pinned runner image reference: {image_ref!r}")
    return image_ref


def _labels(config: Any, *, platform_hint: str | None = None) -> dict[str, str] | None:
    if not isinstance(config, dict):
        raise ValueError("image config must be an object")
    if platform_hint is None:
        os_name, architecture = config.get("os"), config.get("architecture")
        if not isinstance(os_name, str) or not isinstance(architecture, str):
            raise ValueError("single-platform image config is missing its OS or architecture")
        actual_platform = f"{os_name}/{architecture}"
        if actual_platform != PLATFORM:
            raise ValueError(f"pinned runner image has platform {actual_platform!r}, expected {PLATFORM!r}")
    else:
        os_name, architecture = config.get("os"), config.get("architecture")
        if (os_name is not None or architecture is not None) and (os_name, architecture) != ("linux", "amd64"):
            raise ValueError(f"platform-map entry {platform_hint!r} contains a mismatched image config")

    image_config = config.get("config", {})
    if not isinstance(image_config, dict):
        raise ValueError("image config's config field must be an object")
    labels = image_config.get("Labels")
    if labels is None:
        return None
    if not isinstance(labels, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in labels.items()):
        raise ValueError("image config labels must be a string-to-string object")
    return labels


def labels_for_platform(image_document: Any, platform: str = PLATFORM) -> dict[str, str] | None:
    """Read labels from a single config or a platform-keyed imagetools result."""
    if not isinstance(image_document, dict):
        raise ValueError("imagetools .Image must be a JSON object")

    if {"os", "architecture", "config"}.intersection(image_document):
        return _labels(image_document)

    if platform not in image_document:
        raise ValueError(f"imagetools .Image has no {platform!r} platform entry")
    selected = image_document[platform]
    return _labels(selected, platform_hint=platform)


def pinned_runner_labels(root: Path, docker: str = "docker") -> dict[str, str] | None:
    result = subprocess.run(
        [docker, "buildx", "imagetools", "inspect", _pinned_runner_ref(root), "--format", "{{json .Image}}"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return labels_for_platform(json.loads(result.stdout))


def needs_build(inputs: Inputs, labels: dict[str, str] | None) -> bool:
    return labels is None or labels.get(INPUTS_LABEL) != inputs.identity


def _write_outputs(path: Path, inputs: Inputs, *, build_required: bool | None = None) -> None:
    values = [("key", inputs.identity), ("tools_path", inputs.tools_path), ("base_ref", inputs.base_ref)]
    if build_required is not None:
        values.append(("needs_build", str(build_required).lower()))
    with path.open("a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in values)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inputs", "plan"):
        subparser = commands.add_parser(command)
        subparser.add_argument("--root", type=Path, default=Path.cwd())
        subparser.add_argument("--output", type=Path, required=True)
        if command == "plan":
            subparser.add_argument("--docker", default="docker")
    verify = commands.add_parser("verify")
    verify.add_argument("--root", type=Path, default=Path.cwd())
    verify.add_argument("--key", required=True)

    args = parser.parse_args(argv)
    if args.command == "verify":
        actual = load_inputs(args.root).identity
        if actual != args.key:
            raise SystemExit(f"runner image inputs changed: expected {args.key}, got {actual}")
        return

    inputs = load_inputs(args.root)
    if args.command == "inputs":
        _write_outputs(args.output, inputs)
        return

    build_required = needs_build(inputs, pinned_runner_labels(args.root, args.docker))
    print(f"runner image: {'build required' if build_required else 'pinned image matches inputs'}", file=sys.stderr)
    _write_outputs(args.output, inputs, build_required=build_required)


if __name__ == "__main__":
    main()
