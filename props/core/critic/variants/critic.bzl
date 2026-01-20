"""Shared Bazel rules for building critic agent variants.

TODO: Migrate these variants to the new in-container agent loop model.
Currently they use the old /init entrypoint with app_tar. The new model
uses a main.py entrypoint with main_tar (see //props/core/critic:image).

Each critic variant differs only in its agent.md file. This macro
reduces duplication by generating all standard targets from a single
variant-specific markdown file.
"""

load("@rules_oci//oci:defs.bzl", "oci_image", "oci_load", "oci_push")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")


def critic_variant(name, agent_md):
    """Build a critic variant image with custom agent.md (legacy architecture).

    TODO: Migrate to new in-container agent loop model.

    Generates standard targets for building and publishing a critic variant:
    - :<name> - OCI image
    - :<name>_load - Load into local Docker
    - :<name>_push - Push to local registry

    Args:
        name: Variant name (e.g., "contract-truthfulness")
        agent_md: Source markdown file for agent instructions
    """

    # Rename variant-specific agent.md to standard agent.md
    # Uses genrule + strip_prefix pattern (same as init_script_tar)
    native.genrule(
        name = name + "_agent_md_gen",
        srcs = [agent_md],
        outs = [name + "_md/agent.md"],
        cmd = "cp $< $@",
    )

    # Package renamed agent.md
    pkg_tar(
        name = name + "_agent_md_tar",
        srcs = [":" + name + "_agent_md_gen"],
        package_dir = "/",
        strip_prefix = name + "_md",
    )

    # Build OCI image (legacy architecture with /init entrypoint)
    oci_image(
        name = name,
        base = "@python_slim_linux_amd64",
        entrypoint = ["/init"],
        env = {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        },
        tars = [
            "//props/core/cli:app_tar",
            ":critic_dev_init_tar",
            ":" + name + "_agent_md_tar",
        ],
        workdir = "/workspace",
    )

    # Load into local Docker
    oci_load(
        name = name + "_load",
        image = ":" + name,
        repo_tags = ["critic-agent:" + name],
    )

    # Push to registry
    oci_push(
        name = name + "_push",
        image = ":" + name,
        remote_tags = [name],
        repository = "localhost:5050/critic",
    )
