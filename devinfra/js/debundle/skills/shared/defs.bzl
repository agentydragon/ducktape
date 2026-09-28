"""The reference docs every debundle skill ships."""

load("//skills:defs.bzl", "skill_mapping")

# Mirrors the source layout (README/SPEC at the crate root, docs/ below it) so the
# docs' relative links resolve inside the installed skill; each skill's archive
# test fails on a link that does not.
DEBUNDLE_REFERENCES = [
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle:README.md",
            "//devinfra/js/debundle:SPEC.md",
            "//devinfra/js/debundle/skills/shared:module_shape.md",
            "//devinfra/js/debundle/skills/shared:workflow.md",
        ],
        prefix = "references",
    ),
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle/docs:bazel_integration.md",
            "//devinfra/js/debundle/docs:cli.md",
            "//devinfra/js/debundle/docs:selectors.md",
            "//devinfra/js/debundle/docs:spec_editing.md",
        ],
        prefix = "references/docs",
    ),
]
