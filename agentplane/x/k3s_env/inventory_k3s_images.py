"""Inventory OCI image layouts built from agentplane/x/k3s_env/_IMAGES.

Run after building the image targets in that BUILD file, for example:

    python agentplane/x/k3s_env/inventory_k3s_images.py \
      --bazel-bin bb-out/bazel-out/k8-fastbuild/bin

The script verifies every compressed layer digest and reports image-level byte
totals, per-digest sharing, major file categories, and byte-identical Python
toolchain trees after removing each binary's runfiles prefix.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
import tarfile
from typing import IO, Literal

IMAGES = {
    "action_service": ("//agentplane/action_service:image", "agentplane/action_service/image"),
    "action_service_migrate": (
        "//agentplane/action_service:migration_image",
        "agentplane/action_service/migration_image",
    ),
    "app": ("//agentplane/app:image", "agentplane/app/image"),
    "app_migrate": ("//agentplane/app:migration_image", "agentplane/app/migration_image"),
    "egress": ("//agentplane/egress:image", "agentplane/egress/image"),
    "egress_migrate": ("//agentplane/egress:migration_image", "agentplane/egress/migration_image"),
    "egress_sidecar": ("//agentplane/egress:sidecar_image", "agentplane/egress/sidecar_image"),
    "history_migrate": (
        "//agentplane/history_service/storage:migration_image",
        "agentplane/history_service/storage/migration_image",
    ),
    "history_service": ("//agentplane/history_service:image", "agentplane/history_service/image"),
    "llm_ingress": ("//agentplane/llm_ingress:image", "agentplane/llm_ingress/image"),
    "notification_service": ("//agentplane/notification_service:image", "agentplane/notification_service/image"),
    "notification_service_migrate": (
        "//agentplane/notification_service:migration_image",
        "agentplane/notification_service/migration_image",
    ),
    "oauth_fixture": (
        "//agentplane/action_service/testing:oauth_mcp_server_image",
        "agentplane/action_service/testing/oauth_mcp_server_image",
    ),
    "policy_controller": ("//agentplane/policy_controller:image", "agentplane/policy_controller/image"),
    "runner": ("//agentplane/x/k3s_env:runner_image", "agentplane/x/k3s_env/runner_image"),
    "runner_gateway_go": ("//agentplane/runner_gateway:go_image", "agentplane/runner_gateway/go_image"),
    "runner_gateway_queue_migrate": (
        "//agentplane/runner_gateway/go/migrations:image",
        "agentplane/runner_gateway/go/migrations/image",
    ),
    "sandbox_controller": ("//agentplane/sandbox_controller:image", "agentplane/sandbox_controller/image"),
}

INTERPRETER_REPO = "rules_python++python+python_3_14_x86_64-unknown-linux-gnu/"
PIP_REPO_PREFIX = "rules_python++pip+ducktape_pypi_314_"
TRACKED_DISTRIBUTIONS = ("mypy_extensions", "ast_serialize", "mypy", "sqlalchemy")


def normalize_path(path: str) -> str:
    return path.removeprefix("./").lstrip("/")


def category(path: str) -> str:
    lower = "/" + path.lower().lstrip("/")
    if "site-packages/" in lower or "dist-packages/" in lower:
        return "site-packages"
    if any(component in lower for component in ("/libpython", "/bin/python", "/python3.", "/include/python")):
        return "interpreter"
    if "/_main/" in lower or "/agentplane/" in lower or "/util/" in lower:
        return "app/runfiles"
    return "base/other"


def wheel_distribution(path: str) -> str | None:
    if ".runfiles/" not in path or "site-packages/" not in path:
        return None
    runfiles_repo = path.split(".runfiles/", 1)[1].split("/", 1)[0]
    if not runfiles_repo.startswith(PIP_REPO_PREFIX):
        return None
    suffix = runfiles_repo.removeprefix(PIP_REPO_PREFIX)
    for distribution in TRACKED_DISTRIBUTIONS:
        # Wheel tags follow the normalized distribution name, such as
        # mypy_cp314_cp314_* or mypy_extensions_py3_none_*.
        if re.match(rf"^{re.escape(distribution)}_(?:cp\d|py\d|abi\d|manylinux|macosx|win)", suffix):
            return distribution
    return None


def hash_stream(stream: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def inspect_image(alias: str, label: str, root: pathlib.Path) -> dict:
    layout = root / dict(IMAGES)[alias][1]
    index = json.loads((layout / "index.json").read_text())
    manifest_desc = index["manifests"][0]
    manifest_blob = layout / "blobs/sha256" / manifest_desc["digest"].split(":", 1)[1]
    manifest = json.loads(manifest_blob.read_text())

    visible_files: dict[str, int] = {}
    layers = []
    package_dirs: collections.Counter[str] = collections.Counter()
    tracked_wheels: collections.Counter[str] = collections.Counter()
    toolchain_files: dict[str, tuple[int, str]] = {}
    interpreter_bytecode_files = 0
    interpreter_bytecode_bytes = 0

    for layer in manifest["layers"]:
        digest = layer["digest"]
        blob = layout / "blobs/sha256" / digest.split(":", 1)[1]
        with blob.open("rb") as stream:
            actual_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        assert actual_digest == digest.split(":", 1)[1], (alias, digest, actual_digest)
        mode: Literal["r:gz", "r:"] = "r:gz" if layer["mediaType"].endswith("+gzip") else "r:"
        layer_bytes = 0
        layer_files = 0
        layer_categories: collections.Counter[str] = collections.Counter()
        with tarfile.open(blob, mode) as archive:
            for member in archive:
                path = normalize_path(member.name)
                if member.name.endswith("/.wh..wh..opq") or member.name == ".wh..wh..opq":
                    prefix = path.removesuffix(".wh..wh..opq").rstrip("/")
                    for existing in list(visible_files):
                        if existing.startswith(prefix + "/"):
                            del visible_files[existing]
                    continue
                if "/.wh." in member.name or member.name.startswith(".wh."):
                    parent, _, name = path.rpartition("/")
                    target = "/".join(x for x in (parent, name.removeprefix(".wh.")) if x)
                    visible_files.pop(target, None)
                    continue
                if not member.isfile():
                    continue

                layer_bytes += member.size
                layer_files += 1
                layer_categories[category(path)] += member.size
                visible_files[path] = member.size

                site_prefix = "/site-packages/"
                if site_prefix in "/" + path:
                    tail = ("/" + path).split(site_prefix, 1)[1]
                    package = tail.split("/", 1)[0]
                    if package:
                        package_dirs[package] += member.size
                    distribution = wheel_distribution(path)
                    if distribution:
                        tracked_wheels[distribution] += member.size

                if INTERPRETER_REPO in path and category(path) == "interpreter":
                    suffix = path.split(INTERPRETER_REPO, 1)[1]
                    if suffix.endswith(".pyc"):
                        interpreter_bytecode_files += 1
                        interpreter_bytecode_bytes += member.size
                        continue
                    stream = archive.extractfile(member)
                    assert stream is not None
                    file_digest = hash_stream(stream)
                    toolchain_files[suffix] = (member.size, file_digest)

        layers.append(
            {
                "digest": digest,
                "compressed": layer["size"],
                "unpacked_files": layer_bytes,
                "file_count": layer_files,
                "categories": layer_categories,
            }
        )

    config_digest = manifest["config"]["digest"]
    config_bytes = (layout / "blobs/sha256" / config_digest.split(":", 1)[1]).stat().st_size
    return {
        "alias": alias,
        "label": label,
        "layout": str(layout),
        "manifest_digest": manifest_desc["digest"],
        "config_bytes": config_bytes,
        "layers": layers,
        "compressed_layers": sum(layer["compressed"] for layer in layers),
        "unpacked_layer_file_bytes": sum(layer["unpacked_files"] for layer in layers),
        "final_regular_file_bytes": sum(visible_files.values()),
        "final_files": len(visible_files),
        "categories": sum((layer["categories"] for layer in layers), collections.Counter()),
        "largest_packages": package_dirs.most_common(15),
        "tracked_wheels": tracked_wheels,
        "toolchain_files": toolchain_files,
        "interpreter_bytecode_files": interpreter_bytecode_files,
        "interpreter_bytecode_bytes": interpreter_bytecode_bytes,
    }


def render_markdown(images: list[dict]) -> str:
    layer_images: dict[str, set[str]] = collections.defaultdict(set)
    layer_sizes: dict[str, int] = {}
    layer_unpacked: dict[str, int] = {}
    for image in images:
        for layer in image["layers"]:
            digest = layer["digest"]
            layer_images[digest].add(image["alias"])
            layer_sizes[digest] = layer["compressed"]
            layer_unpacked[digest] = layer["unpacked_files"]

    total_compressed = sum(image["compressed_layers"] for image in images)
    unique_compressed = sum(layer_sizes.values())
    total_descriptors = sum(len(image["layers"]) for image in images)
    total_final_files = sum(image["final_regular_file_bytes"] for image in images)
    lines = [
        "# Agentplane k3s OCI image inventory",
        "",
        "Measured from the image targets in `agentplane/x/k3s_env/BUILD.bazel` and `inventory_k3s_images.py`. OCI config/manifest/index metadata is excluded from compressed-layer totals. The script verifies each layer blob SHA-256 and applies whiteouts when computing final visible files.",
        "",
        "## Image totals",
        "",
        "| Image | Bazel label | Compressed layers (B) | Final regular files (B) | Files | Layers |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| `{image['alias']}` | `{image['label']}` | {image['compressed_layers']:,} | {image['final_regular_file_bytes']:,} | {image['final_files']:,} | {len(image['layers'])} |"
        for image in images
    )
    lines.extend(
        [
            "",
            f"The images sum to **{total_compressed:,} compressed layer bytes** per image and **{unique_compressed:,} bytes across unique layer digests** ({total_compressed - unique_compressed:,} repeated bytes). There are {total_descriptors} layer descriptors and {len(layer_sizes)} unique digests. Final regular-file totals sum to **{total_final_files:,} bytes** across images. These values do not model containerd snapshot metadata, filesystem block allocation, or unpacked lower-layer sharing.",
            "",
            "## Layer digest inventory",
            "",
            "Unpacked bytes below count regular files in each layer tar; repeated digests have one descriptor in each named image.",
            "",
            "| Digest | Compressed (B) | Layer files (B) | Image count | Images |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for digest, names in sorted(layer_images.items(), key=lambda item: (-layer_sizes[item[0]], item[0])):
        lines.append(
            f"| `{digest}` | {layer_sizes[digest]:,} | {layer_unpacked[digest]:,} | {len(names)} | {', '.join(f'`{name}`' for name in sorted(names))} |"
        )

    lines.extend(
        [
            "",
            "## Layer payload by category",
            "",
            "Categories are summed from regular files in layer tar members before whiteouts are applied.",
            "",
            "| Image | App/runfiles (B) | Site-packages (B) | Interpreter (B) | Base/other (B) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for image in images:
        categories = image["categories"]
        lines.append(
            f"| `{image['alias']}` | {categories['app/runfiles']:,} | {categories['site-packages']:,} | {categories['interpreter']:,} | {categories['base/other']:,} |"
        )

    python_images = [image for image in images if image["toolchain_files"]]
    if python_images:
        reference = python_images[0]["toolchain_files"]
        toolchain_equal = all(image["toolchain_files"] == reference for image in python_images)
        toolchain_bytes = sum(size for size, _ in reference.values())
        lines.extend(
            [
                "",
                "## Python runtime and largest packages",
                "",
                f"Compared the interpreter subtree after removing each binary-specific `.runfiles/` prefix through `{INTERPRETER_REPO}` and excluding `.pyc` files: {len(python_images)} images, {len(reference):,} files, {toolchain_bytes:,} bytes, and matching paths/sizes/SHA-256 across images: `{toolchain_equal}`.",
                "",
                "| Image | Largest site-packages contributors (layer payload B) |",
                "| --- | --- |",
            ]
        )
        for image in sorted(python_images, key=lambda item: -item["compressed_layers"]):
            top = ", ".join(f"`{name}` {size:,}" for name, size in image["largest_packages"][:6]) or "(none)"
            lines.append(f"| `{image['alias']}` | {top} |")

        mypy_bytes = {
            image["alias"]: sum(image["tracked_wheels"].get(name, 0) for name in TRACKED_DISTRIBUTIONS)
            for image in python_images
        }
        if any(mypy_bytes.values()):
            lines.extend(
                [
                    "",
                    "Tracked Mypy wheel file bytes by Python image (measured from image layer contents):",
                    "",
                    "| Image | Mypy-related wheel bytes |",
                    "| --- | ---: |",
                ]
            )
            lines.extend(f"| `{alias}` | {size:,} |" for alias, size in mypy_bytes.items())

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bazel-bin",
        type=pathlib.Path,
        default=pathlib.Path("bazel-bin"),
        help="Bazel bin directory containing the OCI layouts (default: bazel-bin)",
    )
    args = parser.parse_args()

    missing = [
        (alias, label, args.bazel_bin / output)
        for alias, (label, output) in IMAGES.items()
        if not (args.bazel_bin / output / "index.json").is_file()
    ]
    if missing:
        descriptions = ", ".join(f"{alias} ({path})" for alias, _, path in missing)
        raise SystemExit(f"Missing OCI layout(s): {descriptions}")

    images = [inspect_image(alias, label, args.bazel_bin) for alias, (label, _) in IMAGES.items()]
    print(render_markdown(images))


if __name__ == "__main__":
    main()
