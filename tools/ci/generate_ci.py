"""Generate .github/workflows/ from workflows.yaml.

Generates ci.yml and per-package release workflow files,
eliminating duplication in job definitions.

Usage:
    bazel run //tools/ci:generate_ci_bin
"""

from __future__ import annotations

from pathlib import Path

import yaml

from bazel_util.workspace import get_build_workspace_directory
from tools.ci.github_actions import Job, Step, Workflow
from tools.ci.models import ReleaseConfig, WorkflowConfig, WorkflowManifest

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
WORKFLOWS_YAML = SCRIPT_DIR / "workflows.yaml"
# Runfiles path — used by tests to check generated files against expectations
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"


HEADER = """\
# AUTO-GENERATED from tools/ci/workflows.yaml - DO NOT EDIT DIRECTLY
# Regenerate with: bazel run //tools/ci:generate_ci_bin
"""

BAZEL_DIFF_VERSION = "12.1.1"


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
        Step(uses="actions/checkout@v4", with_args={"fetch-depth": 0}),
        Step(uses="astral-sh/setup-uv@v4"),
        # Set up Bazelisk
        Step(uses="./.github/actions/bazel-repo-cache"),
        Step(uses="actions/setup-java@v4", with_args={"distribution": "temurin", "java-version": "21"}),
        Step(
            name="Cache bazel-diff JAR",
            id="cache-bazel-diff",
            uses="actions/cache@v4",
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
            uses="actions/cache@v4",
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
            'echo "CI_WORKFLOWS_MANIFEST=$PWD/tools/ci/workflows.yaml" >> $GITHUB_ENV\n'
            "# Full build on main/devel pushes; incremental on PRs and feature branches\n"
            'if [[ "$GITHUB_EVENT_NAME" == "push" && "$GITHUB_REF_NAME" =~ ^(main|master|devel)$ ]]; then\n'
            '  echo "CI_PUSH_STRATEGY=full" >> $GITHUB_ENV\n'
            "else\n"
            '  echo "CI_PUSH_STRATEGY=incremental" >> $GITHUB_ENV\n'
            "fi",
        ),
        Step(name="Compute CI decision", id="decide", run="uv run tools/ci/ci_decide.py"),
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
    return name != RBE_IMAGE_JOB and config.secrets == "inherit"


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

    # Jobs that push container images need packages:write.
    image_jobs = {"rbe-image", "props-backend-image"}
    permissions: dict[str, str] = {"contents": "read"}
    if image_jobs & manifest.workflows.keys():
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


def generate_release_config(name: str, config: ReleaseConfig) -> Workflow:
    """Generate a release workflow for a package."""
    latest_release_tag = f"{name}-latest"

    check_job = Job(
        name="Check if release needed",
        runs_on="ubuntu-latest",
        outputs={"release_needed": "${{ steps.check.outputs.release_needed }}"},
        steps=[
            Step(name="Check out code", uses="actions/checkout@v4", with_args={"fetch-depth": 0}),
            Step(
                uses="./.github/actions/setup-bazel",
                id="bazel",
                with_args={"buildbuddy_api_key": "${{ secrets.BUILDBUDDY_API_KEY }}"},
            ),
            Step(
                name="Check if release needed",
                id="check",
                uses="./.github/actions/check-release-needed",
                with_args={
                    "package_prefix": name,
                    "bazel_target": config.bazel_target,
                    "latest_release_tag": latest_release_tag,
                },
            ),
        ],
    )

    release_with: dict[str, str] = {
        "package_name": name,
        "wheel_name": name.replace("-", "_"),
        "bazel_target": config.bazel_target,
        "wheel_path": config.wheel_path,
        "release_body": config.release_body,
        "latest_release_tag": latest_release_tag,
    }
    if config.apt_packages:
        release_with["apt_packages"] = " ".join(config.apt_packages)

    release_job = Job(
        needs="check",
        if_cond="needs.check.outputs.release_needed == 'true' || inputs.force_release == true",
        uses="./.github/workflows/python-wheel-release.yml",
        with_args=release_with,
        secrets="inherit",
    )

    return Workflow(
        name=f"{name} Release",
        on={
            "push": {"branches": ["devel", "main"]},
            "workflow_dispatch": {
                "inputs": {
                    "force_release": {
                        "description": "Force release even if no changes detected",
                        "required": False,
                        "default": False,
                        "type": "boolean",
                    }
                }
            },
        },
        concurrency={"group": "${{ github.workflow }}-${{ github.ref }}", "cancel-in-progress": True},
        permissions={"contents": "write"},
        jobs={"check": check_job, "release": release_job},
    )


def write_workflow(path: Path, workflow: Workflow) -> None:
    """Write a workflow file."""
    path.write_text(generate_ci_yml(workflow))
    print(f"Generated {path}")


def main() -> None:
    """Main entry point."""
    manifest = WorkflowManifest.from_yaml(WORKFLOWS_YAML)

    out_dir = get_build_workspace_directory() / ".github" / "workflows"
    write_workflow(out_dir / "ci.yml", generate_ci_config(manifest))
    for name, config in manifest.releases.items():
        write_workflow(out_dir / f"{name}-release.yml", generate_release_config(name, config))


if __name__ == "__main__":
    main()
