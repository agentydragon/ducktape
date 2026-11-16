"""Generate a callback-registration bundle and its residual-only spec."""

import argparse
import json
from pathlib import Path


def source(callbacks: int, invocations: int, *, attach_method: bool) -> str:
    lines = ["function make(read) {", "  const callback = () => read();"]
    if attach_method:
        lines.append('  Object.defineProperty(callback, "read", { value: read });')
    lines.extend(["  return callback;", "}"])
    lines.extend(f"const callback{i} = make(() => {i});" for i in range(callbacks))
    lines.extend(f"const result{i} = callback0();" for i in range(invocations))
    last = f"callback{callbacks - 1}.read()" if attach_method else f"callback{callbacks - 1}()"
    lines.append(f"console.log(result{invocations - 1}, {last});")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--out", type=Path, help="Write a runnable corpus and spec here")
    output.add_argument("--source-out", type=Path, help="Write only the input JavaScript")
    parser.add_argument("--callbacks", type=int, default=1024)
    parser.add_argument("--invocations", type=int, default=1)
    parser.add_argument("--omit-attached-method", action="store_true")
    args = parser.parse_args()
    if args.callbacks < 1 or args.invocations < 1:
        parser.error("--callbacks and --invocations must be positive")
    bundle = source(args.callbacks, args.invocations, attach_method=not args.omit_attached_method)
    if args.source_out is not None:
        args.source_out.parent.mkdir(parents=True, exist_ok=True)
        args.source_out.write_text(bundle)
        return

    root = args.out.resolve()
    snapshot = root / "snapshot"
    (snapshot / "static").mkdir(parents=True, exist_ok=True)
    (snapshot / "static/app.js").write_text(bundle)
    (snapshot / "package.json").write_text('{"type":"module"}\n')
    (root / "js-files.txt").write_text("static/app.js\n")
    spec = {
        "inputs": {"input_root": str(snapshot), "js_list_path": str(root / "js-files.txt")},
        "logical_modules": {"static/app": {}},
        "unassigned_mode": {"static/app": {"kind": "inline_in_entry"}},
        "materialize_logical_modules": {
            "prune_other_chunks": False,
            "target_dir": "modules",
            "report_out_dir": str(root / "out/reports/tree"),
        },
        "write_js_tree": {"out_dir": str(root / "out")},
    }
    (root / "spec.json").write_text(json.dumps(spec, indent=2) + "\n")


if __name__ == "__main__":
    main()
