"""Generate .github/workflows/ from workflows.yaml.

Generates ci.yml and per-package release workflow files,
eliminating duplication in job definitions.

Usage:
    bazel run //devinfra/ci:generate_ci_bin
"""

from __future__ import annotations

from pathlib import Path

import yaml

from devinfra.ci.diff_utils import BAZEL_DIFF_VERSION
from devinfra.ci.github_actions import Job, Step, Workflow
from devinfra.ci.models import HarborImageConfig, ReleaseConfig, WorkflowConfig, WorkflowManifest
from devinfra.prettier import prettier_format_in_place
from util.bazel.workspace import BazelLabel, get_build_workspace_directory

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
# Runfiles path — used by tests to check generated files against expectations
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


HEADER = """\
# AUTO-GENERATED from devinfra/ci/workflows.yaml - DO NOT EDIT DIRECTLY
# Regenerate with: bazel run //devinfra/ci:generate_ci_bin
"""


COMPUTE_TARGETS_JOB = Job(
    name="Compute affected targets",
    runs_on="ubuntu-latest",
    timeout_minutes=30,
    outputs={
        "targets": "${{ steps.decide.outputs.targets }}",
        "workflows": "${{ steps.decide.outputs.workflows }}",
        "infra_changed": "${{ steps.decide.outputs.infra_changed }}",
    },
    steps=[
        Step(uses="actions/checkout@v6", with_args={"fetch-depth": 0}),
        Step(uses="astral-sh/setup-uv@v7"),
        # Set up Bazelisk
        Step(uses="./.github/actions/bazel-repo-cache"),
        Step(uses="actions/setup-java@v5", with_args={"distribution": "temurin", "java-version": "21"}),
        Step(
            name="Cache bazel-diff JAR",
            id="cache-bazel-diff",
            uses="actions/cache@v5",
            with_args={"path": "bazel-diff.jar", "key": f"bazel-diff-{BAZEL_DIFF_VERSION}"},
        ),
        Step(
            name="Download bazel-diff",
            if_cond="steps.cache-bazel-diff.outputs.cache-hit != 'true'",
            run=(
                f"curl -fsSL -o bazel-diff.jar \\\n"
                f'  "https://github.com/Tinder/bazel-diff/releases/download/{BAZEL_DIFF_VERSION}/bazel-diff_deploy.jar"'
            ),
        ),
        Step(
            name="Cache bazel-diff hashes",
            uses="actions/cache@v5",
            with_args={
                "path": ".bazel-diff-cache",
                "key": "bazel-diff-hashes-${{ github.sha }}",
                "restore-keys": "bazel-diff-hashes-",
            },
        ),
        Step(
            name="Set CI env",
            run='echo "BAZEL_DIFF_JAR=$PWD/bazel-diff.jar" >> $GITHUB_ENV\n'
            'echo "BAZEL_DIFF_CACHE_DIR=$PWD/.bazel-diff-cache" >> $GITHUB_ENV\n'
            'echo "BAZEL_QUERY_LOG_DIR=$PWD/bazel-query-logs" >> $GITHUB_ENV\n'
            'echo "CI_WORKFLOWS_MANIFEST=$PWD/devinfra/ci/workflows.yaml" >> $GITHUB_ENV\n'
            "# Full build on main/master pushes; incremental everywhere else (PRs, devel, feature branches)\n"
            'if [[ "$GITHUB_EVENT_NAME" == "push" && "$GITHUB_REF_NAME" =~ ^(main|master)$ ]]; then\n'
            '  echo "CI_PUSH_STRATEGY=full" >> $GITHUB_ENV\n'
            "else\n"
            '  echo "CI_PUSH_STRATEGY=incremental" >> $GITHUB_ENV\n'
            "fi",
        ),
        Step(name="Compute CI decision", id="decide", run="uv run devinfra/ci/ci_decide.py"),
        Step(
            name="Upload targets files",
            if_cond="always()",
            uses="actions/upload-artifact@v4",
            # Includes targets.txt (all affected) and targets-<workflow>.txt
            # (per-workflow, computed from each workflow's bazel query).
            with_args={"name": "targets", "path": "targets*.txt", "if-no-files-found": "ignore"},
        ),
        Step(
            name="Upload query logs",
            if_cond="always()",
            uses="actions/upload-artifact@v4",
            with_args={
                "name": "bazel-query-logs-${{ github.run_id }}",
                "path": "bazel-query-logs",
                "if-no-files-found": "ignore",
            },
        ),
    ],
)


RBE_IMAGE_JOB = "rbe-image"


def _uses_rbe(name: str, config: WorkflowConfig) -> bool:
    """Whether this workflow uses BuildBuddy RBE and should receive rbe_image."""
    return name != RBE_IMAGE_JOB and config.secrets == "inherit" and config.rbe


def build_workflow_job(name: str, config: WorkflowConfig, *, has_rbe_image_job: bool) -> Job:
    """Build a job definition from workflow config."""
    with_args: dict[str, str] = {}
    if config.targets:
        with_args["targets"] = "${{ needs.compute-targets.outputs.targets }}"
    if config.inputs:
        with_args.update(config.inputs)

    needs: str | list[str] = "compute-targets"
    if_cond = f"contains(fromJson(needs.compute-targets.outputs.workflows || '[]'), '{name}')"

    # Bazel workflows that use RBE should wait for the rbe-image job (when
    # it exists) and forward the built image reference. The job may be skipped
    # when no RBE image files changed, so we allow skipped results.
    if has_rbe_image_job and _uses_rbe(name, config):
        needs = ["compute-targets", RBE_IMAGE_JOB]
        if_cond = (
            f"always() && !cancelled() && !failure() "
            f"&& contains(fromJson(needs.compute-targets.outputs.workflows || '[]'), '{name}')"
        )
        with_args["rbe_image"] = f"${{{{ needs.{RBE_IMAGE_JOB}.outputs.rbe_image }}}}"

    return Job(
        needs=needs,
        if_cond=if_cond,
        uses=f"./.github/workflows/{name}.yml",
        with_args=with_args if with_args else None,
        secrets=config.secrets,
    )


def generate_ci_config(manifest: WorkflowManifest) -> Workflow:
    """Generate the complete ci.yml config."""
    has_rbe_image_job = RBE_IMAGE_JOB in manifest.workflows

    jobs: dict[str, Job] = {"compute-targets": COMPUTE_TARGETS_JOB}
    for name, config in manifest.workflows.items():
        jobs[name] = build_workflow_job(name, config, has_rbe_image_job=has_rbe_image_job)

    # Jobs that push to GHCR need packages:write.
    ghcr_jobs = {"rbe-image", "e2e-container-image"}
    permissions: dict[str, str] = {"contents": "read"}
    if ghcr_jobs & manifest.workflows.keys():
        permissions["packages"] = "write"

    return Workflow(
        name="CI",
        on={"push": {"branches": ["main", "master", "devel"]}, "pull_request": None, "workflow_dispatch": None},
        concurrency={"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True},
        permissions=permissions,
        jobs=jobs,
    )


def generate_ci_yml(workflow: Workflow) -> str:
    """Generate the complete ci.yml content."""
    config = workflow.model_dump(by_alias=True, exclude_none=True)

    # Custom representer for multiline strings
    def str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, str_representer)

    yaml_content = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return HEADER + yaml_content


def _pkg_id(name: str) -> str:
    """Convert package name to a valid GitHub Actions identifier (e.g., gterm-theme -> gterm_theme)."""
    return name.replace("-", "_")


def _binary_dist_path(label: BazelLabel) -> str:
    """Compute the bazel-bin path for a binary target.

    rules_go appends a _ suffix to the output directory for go_binary targets.
    """
    return f"bazel-bin/{label.package}/{label.name}_/{label.name}"


def _tarball_dist_path(label: BazelLabel) -> str:
    """Compute the bazel-bin path for a pkg_tar target."""
    return f"bazel-bin/{label.package}/{label.name}.tar"


def generate_consolidated_release(releases: dict[str, ReleaseConfig]) -> Workflow:
    """Generate the consolidated release.yml workflow."""
    pkg_names = list(releases.keys())

    # --- check-releases job ---
    check_steps: list[Step] = [
        Step(name="Check out code", uses="actions/checkout@v6", with_args={"fetch-depth": 0}),
        Step(
            uses="./.github/actions/setup-bazel",
            id="bazel",
            with_args={"buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}"},
        ),
    ]
    check_outputs: dict[str, str] = {}
    for name, config in releases.items():
        pid = _pkg_id(name)
        check_steps.append(
            Step(
                name=f"Check {name}",
                id=f"check-{pid}",
                uses="./.github/actions/check-release-needed",
                with_args={
                    "package_prefix": name,
                    "bazel_targets": " ".join(str(t.bazel_target) for t in config.targets),
                    "latest_release_tag": f"{name}-latest",
                },
            )
        )
        check_outputs[f"{pid}_needed"] = f"${{{{ steps.check-{pid}.outputs.release_needed }}}}"

    check_job = Job(
        name="Check releases", runs_on="ubuntu-latest", timeout_minutes=30, outputs=check_outputs, steps=check_steps
    )

    jobs: dict[str, Job] = {"check-releases": check_job}

    # --- per-package test jobs ---
    for name, config in releases.items():
        pid = _pkg_id(name)
        if_needed = (
            f"always() && !cancelled() && !failure() && "
            f"(needs.check-releases.outputs.{pid}_needed == 'true'"
            f" || (github.event_name == 'workflow_dispatch'"
            f" && (inputs.package == 'all' || inputs.package == '{name}')))"
        )

        if config.test_targets:
            test_steps: list[Step] = [
                Step(uses="actions/checkout@v6"),
                Step(
                    uses="./.github/actions/setup-bazel",
                    id="bazel",
                    with_args={"buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}"},
                ),
                Step(
                    name="Run tests",
                    run=(
                        f"bazel test --keep_going \\\n"
                        f"  --test_output=all \\\n"
                        f"  --nocache_test_results \\\n"
                        f"  --test_env=CI=true \\\n"
                        f"  {config.test_targets}"
                    ),
                ),
                Step(
                    uses="./.github/actions/collect-test-logs",
                    if_cond="always()",
                    with_args={"artifact-name": f"{name}-test-logs"},
                ),
            ]
            # For ducktape, the standard test job excludes e2e (those are in extra_jobs)
            if config.extra_jobs:
                test_steps[2] = Step(
                    name="Run tests",
                    run=(
                        f"bazel test --keep_going \\\n"
                        f"  --test_output=all \\\n"
                        f"  --nocache_test_results \\\n"
                        f"  --test_tag_filters=-e2e \\\n"
                        f"  --test_env=CI=true \\\n"
                        f"  --test_env=GITHUB_ACTIONS=true \\\n"
                        f"  {config.test_targets}"
                    ),
                )
            jobs[f"test-{pid}"] = Job(
                name=f"Test {name}",
                needs="check-releases",
                if_cond=if_needed,
                runs_on="ubuntu-latest",
                timeout_minutes=30,
                steps=test_steps,
            )

        # --- extra jobs ---
        for extra_name, extra_config in config.extra_jobs.items():
            extra_steps: list[Step] = []
            for s in extra_config.steps:
                extra_steps.append(
                    Step(name=s.name, id=s.id, uses=s.uses, run=s.run, if_cond=s.if_cond, with_args=s.with_args)
                )
            jobs[extra_name] = Job(
                name=extra_name,
                needs=extra_config.needs if extra_config.needs else "check-releases",
                if_cond=if_needed,
                runs_on=extra_config.runs_on,
                timeout_minutes=extra_config.timeout_minutes,
                steps=extra_steps,
            )

    # --- per-package release jobs ---
    for name, config in releases.items():
        pid = _pkg_id(name)
        latest_tag = f"{name}-latest"

        # Determine dependencies
        release_deps: list[str] = ["check-releases"]
        if config.test_targets:
            release_deps.append(f"test-{pid}")
        release_deps.extend(config.release_needs)

        # Build the if condition: all deps must succeed, and package needed or force
        dep_success = " && ".join(f"needs.{d}.result == 'success'" for d in release_deps if d != "check-releases")
        needed_cond = (
            f"needs.check-releases.outputs.{pid}_needed == 'true'"
            f" || (github.event_name == 'workflow_dispatch'"
            f" && inputs.force == true"
            f" && (inputs.package == 'all' || inputs.package == '{name}'))"
        )
        if dep_success:
            if_cond = f"always() && !cancelled() && {dep_success} && ({needed_cond})"
        else:
            if_cond = f"always() && !cancelled() && !failure() && ({needed_cond})"

        release_steps: list[Step] = [
            Step(uses="actions/checkout@v6"),
            Step(
                uses="./.github/actions/setup-bazel",
                id="bazel",
                with_args={"buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}"},
            ),
            Step(name="Set short SHA", run='echo "SHORT_SHA=${GITHUB_SHA::8}" >> $GITHUB_ENV'),
        ]

        if config.apt_packages:
            release_steps.insert(
                1,
                Step(
                    name="Install system dependencies",
                    run=f"sudo apt-get update && sudo apt-get install -y {' '.join(config.apt_packages)}",
                ),
            )

        if config.artifact_type == "binary":
            labels = [t.bazel_target for t in config.targets]
            dist_paths = [_binary_dist_path(lbl) for lbl in labels]
            noun = "binaries" if len(config.targets) > 1 else "binary"
            files_str = "\n".join(f"dist/{lbl.name}" for lbl in labels)
            prepare_cmds = "mkdir -p dist\n" + "\n".join(f"cp {dp} dist/" for dp in dist_paths)
            release_steps.extend(
                [
                    Step(
                        name=f"Build {noun}",
                        run=f"bazel build --remote_download_toplevel {' '.join(str(lbl) for lbl in labels)}",
                    ),
                    Step(name=f"Prepare {noun}", run=prepare_cmds),
                    Step(
                        name="Create release",
                        uses="softprops/action-gh-release@v2",
                        with_args={
                            "tag_name": f"{name}-${{{{ env.SHORT_SHA }}}}",
                            "name": f"{name} (${{{{ env.SHORT_SHA }}}})",
                            "body": f"{config.release_body}\nCommit: ${{{{ github.sha }}}}\nBranch: ${{{{ github.ref_name }}}}",
                            "files": files_str,
                        },
                    ),
                    Step(
                        uses="./.github/actions/update-latest-release",
                        with_args={
                            "package_prefix": name,
                            "latest_tag": latest_tag,
                            "title": f"{name} (latest)",
                            "body": config.release_body,
                            "files": files_str,
                        },
                    ),
                ]
            )
        elif config.artifact_type == "tarball":
            label = config.targets[0].bazel_target
            tarball_path = _tarball_dist_path(label)
            release_filename = f"{name}.tar"
            release_steps.extend(
                [
                    Step(name="Build tarball", run=f"bazel build --remote_download_toplevel {label}"),
                    Step(name="Prepare tarball", run=f"mkdir -p dist\ncp {tarball_path} dist/{release_filename}"),
                    Step(
                        name="Create release",
                        uses="softprops/action-gh-release@v2",
                        with_args={
                            "tag_name": f"{name}-${{{{ env.SHORT_SHA }}}}",
                            "name": f"{name} (${{{{ env.SHORT_SHA }}}})",
                            "body": f"{config.release_body}\nCommit: ${{{{ github.sha }}}}\nBranch: ${{{{ github.ref_name }}}}",
                            "files": f"dist/{release_filename}",
                        },
                    ),
                    Step(
                        uses="./.github/actions/update-latest-release",
                        with_args={
                            "package_prefix": name,
                            "latest_tag": latest_tag,
                            "title": f"{name} (latest)",
                            "body": config.release_body,
                            "files": f"dist/{release_filename}",
                        },
                    ),
                ]
            )
        else:
            if not config.wheel_name:
                raise ValueError(f"wheel_name must be set for wheel package {name!r}")
            wheel_name = config.wheel_name
            release_steps.extend(
                [
                    Step(
                        name="Build wheel",
                        run=f"bazel build --remote_download_toplevel {config.targets[0].bazel_target}",
                    ),
                    Step(name="Prepare wheel", run=f"mkdir -p dist\ncp {config.wheel_path}/{wheel_name}-*.whl dist/"),
                    Step(
                        name="Create release",
                        uses="softprops/action-gh-release@v2",
                        with_args={
                            "tag_name": f"{name}-${{{{ env.SHORT_SHA }}}}",
                            "name": f"{name} (${{{{ env.SHORT_SHA }}}})",
                            "body": f"{config.release_body}\nCommit: ${{{{ github.sha }}}}\nBranch: ${{{{ github.ref_name }}}}",
                            "files": "dist/*.whl",
                        },
                    ),
                    Step(
                        uses="./.github/actions/update-latest-release",
                        with_args={
                            "package_prefix": name,
                            "latest_tag": latest_tag,
                            "title": f"{name} (latest)",
                            "body": config.release_body,
                            "files": "dist/*.whl",
                        },
                    ),
                ]
            )

        jobs[f"release-{pid}"] = Job(
            name=f"Release {name}",
            needs=release_deps,
            if_cond=if_cond,
            runs_on="ubuntu-latest",
            timeout_minutes=15,
            outputs={
                "released": "true",
                "tag": f"{name}-${{{{ env.SHORT_SHA }}}}",
                "short_sha": "${{ env.SHORT_SHA }}",
            },
            steps=release_steps,
        )

    # --- update-downstream job ---
    release_job_names = [f"release-{_pkg_id(n)}" for n in pkg_names]
    any_released = " ||\n     ".join(f"needs.{j}.outputs.released == 'true'" for j in release_job_names)

    # TODO: Replace sed-based URL rewriting with a structured approach (tree-sitter-nix
    # or nix-manipulator once it supports `formals @ identifier : let_expression` syntax).
    flake_updates: list[str] = []
    for name, config in releases.items():
        # Skip packages with no flake inputs at all
        if not any(t.flake_input for t in config.targets) and not config.update_claude_settings:
            continue
        pid = _pkg_id(name)
        job_name = f"release-{pid}"
        sed_lines = ""
        lock_lines = ""
        for target in config.targets:
            if config.artifact_type == "binary":
                file_pattern = target.bazel_target.name
            elif config.artifact_type == "tarball":
                file_pattern = f"{name}.tar"
            else:
                if not config.wheel_name:
                    raise ValueError(f"wheel_name must be set for wheel package {name!r}")
                file_pattern = config.wheel_name
            if target.flake_input:
                sed_lines += (
                    f"  sed -i 's|releases/download/{name}-[^/]*/{file_pattern}|"
                    f"releases/download/${{{{ needs.{job_name}.outputs.tag }}}}/{file_pattern}|' flake.nix\n"
                )
                lock_lines += f"  update_flake_input {target.flake_input}\n"
        if config.update_claude_settings:
            # Update settings.json BEFORE nix flake lock so it succeeds even if nix fails
            settings_file_pattern = name.replace("-", "_")
            sed_lines += (
                f'  sed -i "s|releases/download/{name}-[a-f0-9]*/{settings_file_pattern}|'
                f'releases/download/${{{{ needs.{job_name}.outputs.tag }}}}/{settings_file_pattern}|g"'
                f" .claude/settings.json\n"
            )
        flake_updates.append(
            f'if [ "${{{{ needs.{job_name}.outputs.released }}}}" = "true" ]; then\n{sed_lines}{lock_lines}fi'
        )

    # Preamble: helper function for resilient flake input updates
    preamble = (
        "FAILED_INPUTS=()\n"
        "update_flake_input() {\n"
        '  local input_name="$1"\n'
        '  if nix flake update "$input_name"; then\n'
        '    echo "Updated flake input: $input_name"\n'
        "  else\n"
        '    echo "::warning::Failed to update flake input: $input_name"\n'
        '    FAILED_INPUTS+=("$input_name")\n'
        "  fi\n"
        "}\n"
    )

    downstream_run = preamble + "\n".join(flake_updates)

    downstream_run += (
        '\n\ngit config user.name "github-actions[bot]"\n'
        'git config user.email "41898282+github-actions[bot]@users.noreply.github.com"\n'
        "\n"
        "git add flake.nix flake.lock .claude/settings.json\n"
        "\n"
        "if git diff --cached --quiet; then\n"
        '  echo "No downstream changes"\n'
        "  if [ ${#FAILED_INPUTS[@]} -gt 0 ]; then\n"
        '    echo "::error::Failed to update flake inputs: ${FAILED_INPUTS[*]}"\n'
        "    exit 1\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "\n"
        'git commit -m "chore: bump release artifacts [skip ci]"\n'
        "\n"
        'BRANCH="${{ github.ref_name }}"\n'
        'git pull --rebase origin "$BRANCH"\n'
        'git push origin "HEAD:${BRANCH}"\n'
        "\n"
        "if [ ${#FAILED_INPUTS[@]} -gt 0 ]; then\n"
        '  echo "::error::Partial update committed, but failed to update: ${FAILED_INPUTS[*]}"\n'
        "  exit 1\n"
        "fi"
    )

    jobs["update-downstream"] = Job(
        name="Update downstream references",
        needs=release_job_names,
        if_cond=f"always() && !cancelled() &&\n     ({any_released})",
        runs_on="ubuntu-latest",
        timeout_minutes=15,
        steps=[
            Step(uses="actions/checkout@v6", with_args={"ref": "${{ github.ref_name }}"}),
            Step(uses="cachix/install-nix-action@v30"),
            Step(name="Update references and push", run=downstream_run),
        ],
    )

    # --- build workflow ---
    package_choices = ["all", *pkg_names]
    return Workflow(
        name="Release",
        on={
            "push": {"branches": ["devel", "main"]},
            "workflow_dispatch": {
                "inputs": {
                    "package": {
                        "description": "Package to release",
                        "type": "choice",
                        "options": package_choices,
                        "default": "all",
                    },
                    "force": {
                        "description": "Force release even if no changes detected",
                        "type": "boolean",
                        "default": False,
                    },
                }
            },
        },
        concurrency={"group": "release-${{ github.ref }}", "cancel-in-progress": False},
        permissions={"contents": "write"},
        jobs=jobs,
    )


HARBOR_REGISTRY = "registry.allegedly.works"


def generate_harbor_images_config(images: list[HarborImageConfig]) -> Workflow:
    """Generate the bazel-harbor-images.yml workflow.

    Produces a single workflow that checks out, sets up Bazel, logs in to
    Harbor once, then builds and pushes each image in sequence.
    """
    steps: list[Step] = [
        Step(uses="actions/checkout@v6"),
        Step(
            uses="./.github/actions/setup-bazel",
            with_args={
                "buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}",
                "rbe_image": "${{ inputs.rbe_image }}",
            },
        ),
        Step(
            name="Login to Harbor",
            uses="docker/login-action@v3",
            with_args={
                "registry": HARBOR_REGISTRY,
                "username": "${{ secrets.HARBOR_ROBOT_USERNAME }}",
                "password": "${{ secrets.HARBOR_ROBOT_TOKEN }}",
            },
        ),
    ]
    for img in images:
        image_name = img.local_tag.split(":")[0]
        steps.append(
            Step(
                name=f"Build and push {image_name}",
                run=(
                    f".github/scripts/harbor_push.sh \\\n"
                    f"  {img.bazel_target} \\\n"
                    f"  {img.local_tag} \\\n"
                    f"  {HARBOR_REGISTRY}/{img.remote_path}"
                ),
            )
        )

    job = Job(name="Build and Push", runs_on="ubuntu-latest", timeout_minutes=60, steps=steps)

    return Workflow(
        name="Bazel Harbor Images",
        on={
            "workflow_dispatch": None,
            "workflow_call": {
                "inputs": {
                    "rbe_image": {
                        "description": "Override RBE container image (optional)",
                        "required": False,
                        "type": "string",
                        "default": "",
                    }
                },
                "secrets": {
                    "BUILDBUDDY_API_KEY": {"required": False},
                    "HARBOR_ROBOT_USERNAME": {"required": True},
                    "HARBOR_ROBOT_TOKEN": {"required": True},
                },
            },
        },
        concurrency={"group": "bazel-harbor-images-${{ github.ref }}", "cancel-in-progress": True},
        permissions={"contents": "read"},
        jobs={"build-and-push": job},
    )


def write_workflow(path: Path, workflow: Workflow) -> None:
    """Write a workflow file and run prettier to match pre-commit formatting."""
    path.write_text(generate_ci_yml(workflow))
    prettier_format_in_place(path)
    print(f"Generated {path}")


def main() -> None:
    """Main entry point."""
    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)

    out_dir = get_build_workspace_directory() / ".github" / "workflows"
    write_workflow(out_dir / "ci.yml", generate_ci_config(manifest))
    write_workflow(out_dir / "bazel-harbor-images.yml", generate_harbor_images_config(manifest.harbor_images))
    if manifest.releases:
        write_workflow(out_dir / "release.yml", generate_consolidated_release(manifest.releases))


if __name__ == "__main__":
    main()
