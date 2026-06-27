"""Assemble a fingerprinted SPA bundle from esbuild output plus extra assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


def _url(prefix: str, path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    if prefix.endswith("/"):
        return prefix + normalized
    return prefix + "/" + normalized


def _copy_tree(src: Path, dst: Path) -> None:
    for path in src.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, out)


def _output_relative_to_esbuild_dir(output_path: str, esbuild_dir: Path) -> str:
    normalized = output_path.replace("\\", "/")
    marker = esbuild_dir.name + "/"
    if marker in normalized:
        return normalized.split(marker, 1)[1]
    return normalized


def _entry_output(metafile: dict[str, Any], esbuild_dir: Path) -> str:
    entry_outputs = [
        path
        for path, output in metafile["outputs"].items()
        if output.get("entryPoint") is not None and path.endswith(".js")
    ]
    if len(entry_outputs) != 1:
        raise ValueError(f"expected exactly one JS entry output in esbuild metafile, got {entry_outputs!r}")
    return _output_relative_to_esbuild_dir(entry_outputs[0], esbuild_dir)


def _fingerprint_asset(src: Path, out_dir: Path) -> str:
    digest = hashlib.sha256(src.read_bytes()).hexdigest()[:16]
    stem = src.stem
    suffix = "".join(src.suffixes)
    out_rel = Path("assets") / f"{stem}-{digest}{suffix}"
    out = out_dir / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, out)
    return out_rel.as_posix()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--esbuild-dir", type=Path, required=True)
    parser.add_argument("--metafile", type=Path, required=True)
    parser.add_argument("--index-template", type=Path, required=True)
    parser.add_argument("--asset-manifest", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--entry-placeholder", required=True)
    parser.add_argument("--url-prefix", default="/")
    args = parser.parse_args()

    if args.out_dir.exists():
        shutil.rmtree(args.out_dir)
    args.out_dir.mkdir(parents=True)

    _copy_tree(args.esbuild_dir, args.out_dir)

    metafile = json.loads(args.metafile.read_text(encoding="utf-8"))
    replacements = {args.entry_placeholder: _url(args.url_prefix, _entry_output(metafile, args.esbuild_dir))}

    asset_manifest = json.loads(args.asset_manifest.read_text(encoding="utf-8"))
    for asset in asset_manifest:
        replacements[asset["placeholder"]] = _url(
            args.url_prefix, _fingerprint_asset(Path(asset["path"]), args.out_dir)
        )

    html = args.index_template.read_text(encoding="utf-8")
    for placeholder, replacement in replacements.items():
        if placeholder not in html:
            raise ValueError(f"index template {args.index_template} does not contain placeholder {placeholder!r}")
        html = html.replace(placeholder, replacement)

    leftovers = [placeholder for placeholder in replacements if placeholder in html]
    if leftovers:
        raise ValueError(f"index template still contains unreplaced placeholders: {leftovers!r}")

    (args.out_dir / "index.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
