"""Fetch the built Nix-gated artifacts and print their absolute override paths."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from devinfra.buildbuddy import cli
from devinfra.buildbuddy.ci.bes import Output, merge, read
from devinfra.ci.release_assets import BB_OUT_PREFIX, nix_override_assets


def fetch_overrides(invocation_ids: list[str], artifact_targets: Path) -> dict[str, str]:
    """Resolve assets from each invocation once, download them in one batch, and return pin paths."""
    exports = [(invocation_id, read(invocation_id)) for invocation_id in invocation_ids]
    merged = merge(invocation for _, invocation in exports)
    if merged is None:
        raise SystemExit("--invocations is empty; the build step must expose its bazel_invocation_ids")

    source_by_output: dict[Output, str] = {}
    for invocation_id, invocation in exports:
        for output in invocation.outputs:
            source_by_output.setdefault(output, invocation_id)

    rows = nix_override_assets(artifact_targets, merged.by_label())
    manifest = [
        {"uri": output.uri, "destination": BB_OUT_PREFIX + output.path, "invocation_id": source_by_output[output]}
        for _, output in rows
    ]

    if manifest:
        with tempfile.TemporaryDirectory(prefix="nix-artifact-overrides-") as temp_dir:
            manifest_path = Path(temp_dir) / "manifest.json"
            manifest_path.write_text(json.dumps(manifest))
            try:
                cli.run("blob", "download", "--manifest", str(manifest_path))
            except cli.CLIError as error:
                raise SystemExit(f"failed to download {len(manifest)} Nix artifact(s): {error}") from error

    overrides = {}
    for pin, output in rows:
        path = Path(BB_OUT_PREFIX + output.path)
        if not path.is_file():
            raise SystemExit(f"artifact download completed without creating {path} for pin {pin!r}")
        overrides[pin] = str(path.resolve())
    return overrides


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-targets", type=Path, default=Path("devinfra/ci/artifact_targets.json"))
    parser.add_argument(
        "--invocations", required=True, help="comma-separated invocation ids of the build that produced the assets"
    )
    args = parser.parse_args(argv)
    invocation_ids = [invocation_id.strip() for invocation_id in args.invocations.split(",") if invocation_id.strip()]
    print(json.dumps(fetch_overrides(invocation_ids, args.artifact_targets), sort_keys=True))


if __name__ == "__main__":
    main()
