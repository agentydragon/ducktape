"""Write the JS list and tree config for every chunk of the Excalidraw bundle.

esbuild names shared chunks by content hash, so the chunk roster changes with
the pin. The committed template holds the author decisions; this generator adds
the roster: one JS-list line and one `inline_in_entry` policy per chunk the
template does not already configure. A chunk named in the alias file is copied
under that stable ID, found through the esbuild metafile as the output that
bundles the named module, and listed against its hashed name so imports of the
hashed path resolve to it.
"""

import argparse
import json
import shutil
from pathlib import Path, PurePosixPath

import yaml
from more_itertools import one


def aliases_by_chunk(metafile: Path, chunks: list[str], aliases: dict[str, str]) -> dict[str, str]:
    """Map each aliased chunk's bundle-relative path to its stable chunk ID."""
    outputs = json.loads(metafile.read_text())["outputs"]
    return {
        one(
            chunk
            for chunk in chunks
            for path, output in outputs.items()
            if path.endswith(f"/{chunk}")
            and any(PurePosixPath(i).match(f"node_modules/{module}") for i in output["inputs"])
        ): f"{alias}.js"
        for alias, module in aliases.items()
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--metafile", type=Path, required=True)
    parser.add_argument("--chunks-dir", type=Path, required=True)
    parser.add_argument("--inputs-root", required=True, help="Bin-relative path of --chunks-dir.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--aliases", type=Path, required=True)
    parser.add_argument("--js-list", type=Path, required=True)
    parser.add_argument("--js-list-path", required=True, help="Bin-relative path of --js-list.")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    chunks = sorted(path.relative_to(args.bundle_dir).as_posix() for path in args.bundle_dir.rglob("*.js"))
    if "main.js" not in chunks:
        raise ValueError(f"bundle has no entry chunk: {args.bundle_dir=}")
    aliases = aliases_by_chunk(args.metafile, chunks, yaml.safe_load(args.aliases.read_text()))
    chunk_ids = [aliases.get(chunk, chunk).removesuffix(".js") for chunk in chunks]
    args.chunks_dir.mkdir(parents=True, exist_ok=True)
    for chunk_id, chunk in zip(chunk_ids, chunks, strict=True):
        (args.chunks_dir / f"{chunk_id}.js").parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.bundle_dir / chunk, args.chunks_dir / f"{chunk_id}.js")
    args.js_list.write_text(
        "".join(
            f"{chunk_id}.js\t{chunk}\n" if chunk in aliases else f"{chunk}\n"
            for chunk_id, chunk in zip(chunk_ids, chunks, strict=True)
        )
    )

    config = yaml.safe_load(args.template.read_text())
    config["inputs"] = {"root": args.inputs_root, "js_list_path": args.js_list_path}
    policies = config.setdefault("unassigned_mode", {})
    for chunk_id in chunk_ids:
        policies.setdefault(chunk_id, {"kind": "inline_in_entry"})
    args.config.write_text(yaml.safe_dump(config, sort_keys=False))


if __name__ == "__main__":
    main()
