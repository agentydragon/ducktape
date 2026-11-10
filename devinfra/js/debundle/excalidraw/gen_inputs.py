"""Write the JS list and tree config for every chunk of the Excalidraw bundle.

esbuild names shared chunks by content hash, so the chunk roster changes with
the pin; the committed template holds the author decisions and this generator
adds the roster: one JS-list line and one `inline_in_entry` policy per chunk
the template does not already configure.
"""

import argparse
from pathlib import Path

import yaml


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument("--inputs-root", required=True, help="Bin-relative path of --bundle-dir.")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--js-list", type=Path, required=True)
    parser.add_argument("--js-list-path", required=True, help="Bin-relative path of --js-list.")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()

    chunks = sorted(path.name for path in args.bundle_dir.glob("*.js"))
    if "main.js" not in chunks:
        raise ValueError(f"bundle has no entry chunk: {args.bundle_dir=}")
    args.js_list.write_text("".join(f"{chunk}\n" for chunk in chunks))

    config = yaml.safe_load(args.template.read_text())
    config["inputs"] = {"root": args.inputs_root, "js_list_path": args.js_list_path}
    policies = config.setdefault("unassigned_mode", {})
    for chunk in chunks:
        policies.setdefault(chunk.removesuffix(".js"), {"kind": "inline_in_entry"})
    args.config.write_text(yaml.safe_dump(config, sort_keys=False))


if __name__ == "__main__":
    main()
