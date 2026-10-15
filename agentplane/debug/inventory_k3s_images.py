"""Inventory OCI image layouts built from agentplane/x/k3s_env/_IMAGES.

Run after building the image targets in that BUILD file, for example:

    python agentplane/debug/inventory_k3s_images.py \
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
    "runner_gateway": ("//agentplane/runner_gateway:image", "agentplane/runner_gateway/image"),
    "runner_gateway_migrate": (
        "//agentplane/runner_gateway/commands:migration_image",
        "agentplane/runner_gateway/commands/migration_image",
    ),
    "sandbox_controller": ("//agentplane/sandbox_controller:image", "agentplane/sandbox_controller/image"),
    "sandbox_service": ("//agentplane/sandbox_service:image", "agentplane/sandbox_service/image"),
    "session_dispatcher": ("//agentplane/session_dispatcher:image", "agentplane/session_dispatcher/image"),
    "session_dispatcher_migrate": (
        "//agentplane/session_dispatcher:migration_image",
        "agentplane/session_dispatcher/migration_image",
    ),
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
    repeated = [digest for digest, names in layer_images.items() if len(names) > 1]
    python_images = [image for image in images if image["toolchain_files"]]
    reference = python_images[0]["toolchain_files"]
    toolchain_equal = all(image["toolchain_files"] == reference for image in python_images)
    toolchain_bytes = sum(size for size, _ in reference.values())
    app_image = next(image for image in python_images if image["alias"] == "app")
    mypy_wheel_bytes = {
        image["alias"]: image["tracked_wheels"].get("mypy", 0)
        + image["tracked_wheels"].get("mypy_extensions", 0)
        + image["tracked_wheels"].get("ast_serialize", 0)
        for image in python_images
    }
    mypy_images = [f"`{alias}` {size:,} B" for alias, size in mypy_wheel_bytes.items() if size > 0]

    lines = [
        "# Agentplane k3s OCI image inventory (2026-10-14)",
        "",
        "Measured from the 21 labels in `agentplane/x/k3s_env/BUILD.bazel` at source revision `ff38137b3f`. This predates the later removal of the Sandbox Service image on current `devel`; numbers below describe the measured snapshot, not today's target list. OCI config/manifest/index metadata is excluded from compressed-layer totals. The script verifies each layer blob SHA-256 and counts regular-file bytes from uncompressed tar members. No layer contained whiteouts or replaced paths in this snapshot.",
        "",
        "## Image totals",
        "",
        "| Image | Bazel label | Compressed layers (B) | Unpacked regular files (B) | Files | Layers |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    lines.extend(
        (
            f"| `{image['alias']}` | `{image['label']}` | {image['compressed_layers']:,} | {image['final_regular_file_bytes']:,} | {image['final_files']:,} | {len(image['layers'])} |"
        )
        for image in images
    )
    lines.extend(
        [
            "",
            f"All images sum to **{total_compressed:,} compressed layer bytes** per image and **{unique_compressed:,} bytes across unique layer digests** ({total_compressed - unique_compressed:,} duplicate bytes). There are {sum(len(image['layers']) for image in images)} image-layer descriptors and {len(layer_sizes)} unique layer digests; {len(repeated)} digests are shared. The summed unpacked regular-file bytes are **{sum(image['unpacked_layer_file_bytes'] for image in images):,}** across image contents. This is not containerd's disk footprint: it does not model snapshotter metadata, filesystem block allocation, or unpacked lower-layer sharing.",
            "",
            "## Layer digest inventory",
            "",
            "Every unique compressed digest is listed below. Unpacked bytes are regular files in the layer tar; shared entries have one identical descriptor in each named image.",
            "",
            "| Digest | Compressed (B) | Unpacked regular files (B) | Image count | Images |",
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
            "## Runtime and largest contributors",
            "",
            f"The 19 Python images each have an interpreter layer with a unique OCI digest. After stripping each binary-specific `.runfiles/` path through `{INTERPRETER_REPO}` and excluding `.pyc` cache files, every one has the same **{len(reference):,} files ({toolchain_bytes:,} unpacked bytes)** by canonical path, size and SHA-256: `{toolchain_equal}`. The `app` image additionally carries {app_image['interpreter_bytecode_files']:,} standard-library `.pyc` cache files ({app_image['interpreter_bytecode_bytes']:,} bytes); those remain app-specific. The full layers are about 212.32 MB compressed apiece and also contain per-image files. Repackaging the matching interpreter subtree as one shared layer therefore has an upper bound of 18 duplicate copies, **{18 * toolchain_bytes:,} unpacked bytes**; compressed savings require measuring the prototype because the current compressed layers mix in per-binary files.",
            "",
            "| Image | Largest site-packages contributors (unpacked B) |",
            "| --- | --- |",
        ]
    )
    for image in sorted(python_images, key=lambda item: -item["compressed_layers"]):
        top = ", ".join(f"`{name}` {size:,}" for name, size in image["largest_packages"][:6])
        lines.append(f"| `{image['alias']}` | {top} |")

    lines.extend(
        [
            "",
            "| Image | App/runfiles (unpacked B) | Site-packages (unpacked B) | Interpreter (unpacked B) | Base/other (unpacked B) |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for image in sorted(images, key=lambda item: -item["compressed_layers"]):
        cats = image["categories"]
        lines.append(
            f"| `{image['alias']}` | {cats['app/runfiles']:,} | {cats['site-packages']:,} | {cats['interpreter']:,} | {cats['base/other']:,} |"
        )

    lines.extend(
        [
            "",
            "The runner is the largest image (**882,908,112 compressed / 1,440,137,943 unpacked bytes**). Its explicit Claude/Codex harness archive contributes 605,624,320 compressed / 605,534,845 unpacked bytes; `agentplane/x/k3s_env/BUILD.bazel` documents these binaries as required by the Runner SandboxTemplate. It is an intentional image-specific payload, not a generic cleanup candidate. The native Go policy and sandbox controllers are much smaller at 63,072,658 and 76,548,498 compressed bytes.",
            "",
            "The `app` image's largest package contributors are `kubernetes_asyncio` (42,472,505 B), a 36,810,400 B compiled `mypy` extension, SQLAlchemy (23,155,890 B), `glide_shared` (19,832,877 B), `glide` (19,713,242 B), and gRPC (18,691,941 B). Migration package layers range from 28,474,020 to 46,582,997 compressed bytes (101,679,257 to 167,085,712 unpacked), below their paired service images. Alembic, SQLAlchemy, PostgreSQL drivers, and application DB models are in the migration closures and are required by those entrypoints; the image inventory does not justify dropping them.",
            "",
            "## Suspicious dependency: SQLAlchemy's `mypy` extra",
            "",
            f"{sum(size > 0 for size in mypy_wheel_bytes.values())} of the 19 Python images include files from a Mypy wheel runfiles repository. Summed across the `mypy`, `mypy_extensions`, and `ast_serialize` wheel roots, those files contribute **{min(mypy_wheel_bytes.values()):,} to {max(mypy_wheel_bytes.values()):,} unpacked bytes per Python image** (19-image total {sum(mypy_wheel_bytes.values()):,} bytes). Measured image totals: {', '.join(mypy_images)}. In the action-service migration, the Mypy wheel contains the 36,810,400 B `08ae81f72d5a2b5fa9e0__mypyc.cpython-314-x86_64-linux-gnu.so` extension, plus 9,498,416 B under `mypy`, 3,853,335 B under `mypyc`, and 3,066,411 B under `ast_serialize`.",
            "",
            'The path is declared in source, rather than guessed from matching directory names: `//agentplane/action_service:migration_image_bin` depends on `:database_migrate`, whose `:db` closure depends on `@pypi//sqlalchemy`; `pyproject.toml` declares `sqlalchemy[mypy]`, and the generated `@pypi//sqlalchemy` wheel BUILD sets `extras = ["asyncio", "mypy"]` with `mypy>=0.910; extra == "mypy"`. The lock also records `mypy==2.3.1` as `via sqlalchemy`. SQLAlchemy\'s Mypy plugin serves static type checking; its optional `mypy` extra is not needed to run migrations. The repository\'s dedicated `//devinfra/lint:mypy_cli` target separately declares `@pypi//mypy` plus SQLAlchemy for the build-time type checker, so removing the global SQLAlchemy extra must preserve that tool target\'s closure. This is a credible dependency-boundary candidate: narrow the Mypy extra to the type-checking target, then rebuild and test the affected Python libraries/images before claiming savings. The candidate\'s exact unpacked upper bound is reported above; compressed savings remain unmeasured because layer compression mixes the affected wheel files with other packages.',
            "",
            "A BBR `somepath(//agentplane/action_service:migration_image_bin, @pypi//mypy)` query returned empty results (invocation `8c86327e-fc58-4b67-bcda-d2dfa452f5a2`). The generated wheel BUILD and file paths provide the missing explanation: SQLAlchemy's selected optional extra injects Mypy through generated wheel metadata rather than a handwritten `deps` entry. A build with `--config=nolint` retained the same migration manifest digest (`sha256:3cabd9346976037d720a87da8423cea3d8521dcd9f86058eb3b99815b16cd71c`; invocation `5508d804-eaf0-4231-8829-12866269f5b3`), so disabling lint aspects alone does not remove the payload.",
            "",
            "The large Egress-only pieces (`zstandard` ~22.6 MB, `mitmproxy_rs` ~8.1 MB, `aioquic` ~6.4 MB, Brotli ~5.1 MB) correspond to the proxy's protocol/compression implementation. Their size alone is not evidence of an accidental edge. Likewise, the runner harness archives are explicitly required. No language rewrite or speculative removals are proposed.",
            "",
            "## Reproduction",
            "",
            "At the measured source revision `ff38137b3f`, build all 21 image labels from `_IMAGES`, then run:",
            "",
            "```sh",
            "direnv exec . bbr build \\\n  " + " \\\n  ".join(label for label, _ in IMAGES.values()),
            "direnv exec . python agentplane/debug/inventory_k3s_images.py --bazel-bin bb-out/bazel-out/k8-fastbuild/bin",
            "```",
            "",
            "The successful all-image BBR build was invocation `23745169-079f-468d-aded-2d7861d2aea8`; it materialized the OCI layout blobs in the `bb-out` tree. The script uses only the Python standard library and verifies compressed layer SHA-256 before reading tar members. The accepted lifecycle profiles are documented in [the k3s spike README](../x/k3s_env/README.md): baseline profile run `75cf36f3-50c9-423c-bc24-522e00782390` at `4fcc98579e` plus instrumentation, OCI stream run `3b656910-9b6f-4b6c-8a7a-e7129375a1fd` at `ff38137b3f` plus instrumentation. Inventory source revision and profiled source revisions are stated separately.",
            "",
        ]
    )
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
