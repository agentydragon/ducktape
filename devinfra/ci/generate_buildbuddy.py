"""Generate buildbuddy.yaml from the IMAGES list in bb_push_images.

Per-image push actions give independent failure isolation — a broken image
build doesn't block other images from pushing.

Usage:
    bazel run //devinfra/ci:generate_buildbuddy_bin
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from devinfra.ci.bb_push_images import IMAGES
from devinfra.prettier import prettier_format_in_place
from util.bazel.workspace import get_build_workspace_directory

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent.parent
BUILDBUDDY_YAML = REPO_ROOT / "buildbuddy.yaml"

HEADER = """\
# AUTO-GENERATED from devinfra/ci/generate_buildbuddy.py - DO NOT EDIT DIRECTLY
# Regenerate with: bazel run //devinfra/ci:generate_buildbuddy_bin
#
# BuildBuddy docs: https://www.buildbuddy.io/docs/workflows-config/
"""


def generate_buildbuddy_config() -> dict[str, Any]:
    """Generate the complete buildbuddy.yaml config."""
    # Shared Python objects — PyYAML emits YAML anchors/aliases for these
    # automatically, deduplicating the repeated trigger/resource blocks.
    push_triggers: dict[str, Any] = {"push": {"branches": ["main", "devel"]}, "workflow_dispatch": {}}
    push_resources: dict[str, str] = {"disk": "50GB"}

    actions: list[dict[str, Any]] = [
        {
            "name": "CI",
            "triggers": {"push": {"branches": ["main", "master", "devel"]}, "pull_request": {}},
            "container_image": "ubuntu-24.04",
            "resource_requests": {"memory": "16GB", "disk": "50GB"},
            "bazel_commands": [
                (
                    "test --keep_going --config=rbe"
                    " --build_metadata=ROLE=ci --build_metadata=TAGS=test"
                    " --test_tag_filters=-live_openai_api"
                    " //..."
                ),
                (
                    "build --keep_going --config=rbe"
                    " --strategy=mypy=local"
                    " --build_metadata=ROLE=ci --build_metadata=TAGS=check"
                    " //..."
                ),
            ],
        },
        {
            "name": "Release",
            "triggers": push_triggers,
            "container_image": "ubuntu-24.04",
            "resource_requests": {"memory": "16GB", "disk": "50GB"},
            "steps": [
                {
                    "run": (
                        "bazel test --keep_going --config=rbe"
                        " --build_metadata=ROLE=ci"
                        " --test_tag_filters=-live_openai_api"
                        " //...\n"
                    )
                },
                {
                    "run": (
                        "# Install system deps for wheel builds (cairo, dbus, etc.)\n"
                        "sudo apt-get update -qq && sudo apt-get install -y \\\n"
                        "  libcairo2-dev libgirepository-2.0-dev libdbus-1-dev libxcb1-dev pkg-config\n"
                        "bazel build --config=rbe --remote_download_toplevel \\\n"
                        "  //:wheel \\\n"
                        "  //:claude_hooks_wheel \\\n"
                        "  //util:wheel \\\n"
                        "  //gterm_theme:wheel \\\n"
                        "  //skills:all_skills_tar \\\n"
                        "  //devinfra/buildbuddy_cli:bbapi\n"
                        "bazel run //devinfra/ci:bb_release_bin\n"
                    )
                },
            ],
        },
    ]

    for img in IMAGES:
        repo_name = img.repository.rsplit("/", 1)[-1]
        actions.append(
            {
                "name": f"Push {repo_name}",
                "triggers": push_triggers,
                "container_image": "ubuntu-24.04",
                "resource_requests": push_resources,
                "bazel_commands": [
                    f"build --config=rbe --remote_download_toplevel {img.image_target}",
                    f"run --config=rbe //devinfra/ci:bb_push_images_bin -- --image {img.image_target}",
                ],
            }
        )

    return {"actions": actions}


def generate_buildbuddy_yml(config: dict[str, Any]) -> str:
    """Serialize config to YAML with header."""

    def str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml.add_representer(str, str_representer)
    yaml_content = yaml.dump(config, default_flow_style=False, sort_keys=False, allow_unicode=True, width=120)
    return HEADER + yaml_content


def main() -> None:
    config = generate_buildbuddy_config()
    out_path = get_build_workspace_directory() / "buildbuddy.yaml"
    out_path.write_text(generate_buildbuddy_yml(config))
    prettier_format_in_place(out_path)
    print(f"Generated {out_path}")


if __name__ == "__main__":
    main()
