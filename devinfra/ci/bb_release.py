"""BB Release step: create GitHub releases for changed artifacts.

Assumes artifacts are already built by `bazel build` in the calling workflow.
Expects: GH_RELEASE_PAT env var.
"""

import os
import shutil
import subprocess
from pathlib import Path

from github import Auth, Github
from more_itertools import one

from devinfra.ci.artifacts import ARTIFACTS, SOURCES_PATH, Sources, file_sha256

REPO = "agentydragon/ducktape"


def copy_artifact_to_dist(src_glob: str, dest: str) -> Path:
    src_path = Path(src_glob)
    match = one(src_path.parent.glob(src_path.name))
    dest_path = Path(dest)
    if dest_path.is_dir():
        dest_path = dest_path / match.name
    shutil.copy2(match, dest_path)
    return dest_path


def main() -> None:
    # Use git CLI instead of pygit2 — BuildBuddy does partial clones which
    # set extensions.partialclone, unsupported by libgit2/pygit2.
    subject = subprocess.check_output(["git", "log", "-1", "--format=%s"], text=True).strip()
    if "[skip ci]" in subject:
        print("Commit message contains [skip ci], skipping release.")
        return

    gh_token = os.environ.get("GH_RELEASE_PAT")
    if not gh_token:
        raise RuntimeError("Missing required env var: GH_RELEASE_PAT (configure as a BuildBuddy Workflow secret)")

    short_sha = subprocess.check_output(["git", "rev-parse", "--short=7", "HEAD"], text=True).strip()

    Path("dist").mkdir(exist_ok=True)

    sources = Sources.model_validate_json(SOURCES_PATH.read_text())
    gh_repo = Github(auth=Auth.Token(gh_token)).get_repo(REPO)

    changed = []
    for artifact in ARTIFACTS:
        dist_path = copy_artifact_to_dist(artifact.src_glob, artifact.dest)
        pin = sources.pins.get(artifact.pkg)
        if pin and file_sha256(dist_path) == pin.sha256:
            print(f"{artifact.pkg}: unchanged, skipping")
            continue
        tag = f"{artifact.pkg}-{short_sha}"
        print(f"{artifact.pkg}: content changed, creating release {tag}")
        release = gh_repo.create_git_release(
            tag=tag, name=f"{artifact.pkg} ({short_sha})", message=artifact.notes, make_latest="false"
        )
        release.upload_asset(str(dist_path))
        changed.append(artifact.pkg)

    if not changed:
        print("No artifacts changed, skipping release")
        return

    print(f"Released: {' '.join(changed)}")
    print("Pins will be updated by the sync-pins workflow.")


if __name__ == "__main__":
    main()
