"""Fetch the pinned released gazelle binary for the drift check, or decide to skip.

Runs on a plain GitHub Actions runner (stdlib only, no Bazel): reads the `gazelle`
pin from nix/artifact-pins.json, downloads and digest-verifies the binary to
/tmp/gazelle, and emits `skip=<reason>` to GITHUB_OUTPUT instead when the check
cannot be meaningful:

- `bootstrap`: no pin exists yet — the release lands from a devel push and
  sync-pins publishes the pin afterwards, so the first runs after introduction
  skip until the pipeline has cycled once.
- `stale`: a pull request changes the files that define the binary's behavior
  (plugin version, patch, binary composition), so the released binary may
  disagree with the PR's own tree; the devel push after merge validates it.
"""

import base64
import hashlib
import json
import os
import subprocess
import urllib.request
from pathlib import Path

STALE_PATHS = {"MODULE.bazel", "patches/rules_python_gazelle_ducktape.patch", "devinfra/BUILD.bazel"}


def main() -> None:
    pin = json.loads(Path("nix/artifact-pins.json").read_text())["pins"].get("gazelle")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
        if pin is None:
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
                hits = sorted(STALE_PATHS.intersection(changed))
                if hits:
                    print(
                        f"::notice::PR changes gazelle-defining files ({', '.join(hits)}); "
                        "the released binary may be stale for this tree — skipping, validated on devel after merge"
                    )
                    out.write("skip=stale\n")
                    return
    data = urllib.request.urlopen(pin["url"]).read()
    digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
    if digest != pin["sha256"]:
        print(f"::error::gazelle binary digest mismatch: got {digest}, pinned {pin['sha256']}")
        raise SystemExit(1)
    binary = Path("/tmp/gazelle")
    binary.write_bytes(data)
    binary.chmod(0o755)
    print(f"fetched {pin['url']} ({len(data)} bytes, digest OK)")


if __name__ == "__main__":
    main()
