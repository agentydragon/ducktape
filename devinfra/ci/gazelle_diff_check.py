"""Decide whether the gazelle drift check applies to this run.

Runs on a plain GitHub Actions runner (stdlib only, no Bazel) and emits
`skip=<reason>` to GITHUB_OUTPUT when the check cannot be meaningful; the
workflow then skips fetching and running the binary. The binary itself lands
on PATH via `nix profile install .#gazelle` — the flake's `artifacts.gazelle`
fetchurl carries the pin's sha256, so nix is the download and the digest check
in one.

- `bootstrap`: no `gazelle` pin exists in this tree — the release lands from a
  devel push and sync-pins publishes the pin afterwards, so trees from before
  the pin skip until the pipeline has cycled.
- `stale`: a pull request changes the files that define the binary's behavior
  (plugin version, patch, binary composition), so the released binary may
  disagree with the PR's own tree; the devel push after merge validates it.
"""

import json
import os
import subprocess
from pathlib import Path

STALE_PATHS = {"MODULE.bazel", "patches/rules_python_gazelle_ducktape.patch", "devinfra/BUILD.bazel"}


def main() -> None:
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
        if "gazelle" not in json.loads(Path("nix/artifact-pins.json").read_text())["pins"]:
            print(
                "::notice::no released gazelle pin yet; skipping the drift check until the release pipeline has cycled"
            )
            out.write("skip=bootstrap\n")
            return
        if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
            # The checkout is GitHub's synthetic merge; ^1 is the base, so
            # ^1..HEAD is exactly the PR's delta (checkout fetch-depth: 2).
            base = subprocess.run(["git", "rev-parse", "HEAD^1"], capture_output=True, text=True, check=False)
            if base.returncode == 0:
                changed = subprocess.run(
                    ["git", "diff", "--name-only", base.stdout.strip(), "HEAD"],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.split()
                if hits := sorted(STALE_PATHS.intersection(changed)):
                    print(
                        f"::notice::PR changes gazelle-defining files ({', '.join(hits)}); "
                        "the released binary may be stale for this tree — skipping, validated on devel after merge"
                    )
                    out.write("skip=stale\n")


if __name__ == "__main__":
    main()
