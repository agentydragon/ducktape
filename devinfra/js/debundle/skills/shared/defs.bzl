"""The reference docs every debundle skill ships."""

load("//skills:defs.bzl", "skill_mapping")

# Mirrors the source layout (README/SPEC at the crate root, docs/ below it) so the
# docs' relative links resolve inside the installed skill; each skill's archive
# test fails on a link that does not.
def _contract_example_mappings(scenario):
    root = "//devinfra/js/debundle/skills/shared/examples:contract_examples/" + scenario
    prefix = "references/skills/shared/examples/contract_examples/" + scenario
    callback_module = "registration" if scenario == "persistent_registry" else "wrapped"
    control_file = {
        "fresh_factory": "named_callback.js",
        "retained_constructor": "unlisted_arity.js",
        "persistent_registry": "early_emit.js",
    }[scenario]
    return [
        skill_mapping(
            srcs = [
                root + "/js-files.txt",
                root + "/spec_config.yaml",
                root + "/vendor_marks.yaml",
            ],
            prefix = prefix,
        ),
        skill_mapping(
            srcs = [root + "/modules/main/callbacks/" + callback_module + ".yaml"],
            prefix = prefix + "/modules/main/callbacks",
        ),
        skill_mapping(
            srcs = [root + "/modules/main/values/later.yaml"],
            prefix = prefix + "/modules/main/values",
        ),
        skill_mapping(
            srcs = [
                root + "/source/main.js",
                root + "/source/package.json",
            ],
            prefix = prefix + "/source",
        ),
        skill_mapping(
            srcs = [root + "/source/static/peer.js"],
            prefix = prefix + "/source/static",
        ),
        skill_mapping(
            srcs = [root + "/source/controls/" + control_file],
            prefix = prefix + "/source/controls",
        ),
    ]

_CONTRACT_EXAMPLE_MAPPINGS = [
    mapping
    for scenario in ["fresh_factory", "retained_constructor", "persistent_registry"]
    for mapping in _contract_example_mappings(scenario)
]

DEBUNDLE_REFERENCES = [
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle/skills/shared:initialization_contracts.md",
            "//devinfra/js/debundle/skills/shared:module_shape.md",
            "//devinfra/js/debundle/skills/shared:workflow.md",
        ],
        prefix = "references/skills/shared",
    ),
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle/skills/shared/examples:isolated_edit.py",
            "//devinfra/js/debundle/skills/shared/examples:contract_examples.py",
        ],
        prefix = "references/skills/shared/examples",
    ),
] + _CONTRACT_EXAMPLE_MAPPINGS + [
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle:README.md",
            "//devinfra/js/debundle:SPEC.md",
        ],
        prefix = "references",
    ),
    skill_mapping(
        srcs = [
            "//devinfra/js/debundle/docs:architecture_review.md",
            "//devinfra/js/debundle/docs:bazel_integration.md",
            "//devinfra/js/debundle/docs:bug_reproductions.md",
            "//devinfra/js/debundle/docs:cli.md",
            "//devinfra/js/debundle/docs:integration.md",
            "//devinfra/js/debundle/docs:naming.md",
            "//devinfra/js/debundle/docs:planning.md",
            "//devinfra/js/debundle/docs:selectors.md",
            "//devinfra/js/debundle/docs:spec_editing.md",
        ],
        prefix = "references/docs",
    ),
]
