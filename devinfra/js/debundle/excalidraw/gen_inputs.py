"""Write the JS list and tree config for every chunk of the Excalidraw bundle.

esbuild names shared chunks by content hash, so the chunk roster changes with
the pin. The committed template holds the author decisions; this generator adds
the roster: one JS-list line and one `inline_in_entry` policy per chunk the
template does not already configure. A chunk named in the alias file is copied
under that stable ID, found through the esbuild metafile as the output that
bundles the named module, and listed against its hashed name so imports of the
hashed path resolve to it. The page and the bundle's other assets are copied
beside the chunks, where the browser harness reads its snapshot.
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
    parser.add_argument("--index-html", type=Path, required=True)
    parser.add_argument("--js-list", type=Path, required=True)
    parser.add_argument("--js-list-path", required=True, help="Bin-relative path of --js-list.")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    files = sorted(
        path.relative_to(args.bundle_dir).as_posix() for path in args.bundle_dir.rglob("*") if path.is_file()
    )
    chunks = [file for file in files if file.endswith(".js")]
    if "main.js" not in chunks:
        raise ValueError(f"bundle has no entry chunk: {args.bundle_dir=}")
    aliases = aliases_by_chunk(args.metafile, chunks, yaml.safe_load(args.aliases.read_text()))
    for file in files:
        target = args.chunks_dir / aliases.get(file, file)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.bundle_dir / file, target)
    shutil.copyfile(args.index_html, args.chunks_dir / "index.html")
    (args.chunks_dir / "asset-summary.json").write_text(json.dumps({"entryPoints": {"html": "index.html"}}))

    config = yaml.safe_load(args.template.read_text())
    config["inputs"] = {"root": args.inputs_root, "js_list_path": args.js_list_path}
    config["browser_harness"] = {"asset_summary_path": f"{args.inputs_root}/asset-summary.json"}
    policies = config.setdefault("unassigned_mode", {})
    import_orders = config.setdefault("import_order", {})
    js_list = []
    for chunk in chunks:
        staged_name = aliases.get(chunk, chunk)
        js_list.append(f"{staged_name}\t{chunk}\n" if chunk in aliases else f"{chunk}\n")
        chunk_id = staged_name.removesuffix(".js")
        policies.setdefault(chunk_id, {"kind": "inline_in_entry"})
        # Every generated non-authored chunk is explicitly inline, so its
        # emitted non-entry owner universe is empty. Preserve any priorities
        # authored in the template for chunks with logical modules.
        import_orders.setdefault(chunk_id, [])
    args.js_list.write_text("".join(js_list))
    args.config.write_text(yaml.safe_dump(config, sort_keys=False))


if __name__ == "__main__":
    main()
