"""Bazel rules for verifying dependency constraints."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

def assert_no_deps(name, target, forbidden, **kwargs):
    """Verify that target doesn't depend on any of the forbidden labels.

    Creates a genquery + sh_test pair that fails if target has any transitive
    dependencies matching any of the forbidden label patterns.

    Args:
        name: Test name (will also be used as prefix for genquery target)
        target: The target to check dependencies for (e.g., "//pkg:lib")
        forbidden: List of Bazel label patterns to forbid (e.g., ["@pypi//mcp"])
        **kwargs: Additional arguments passed to sh_test (e.g., tags)
    """
    if type(forbidden) != "list":
        fail("forbidden must be a list of label patterns, got: " + type(forbidden))

    query_name = name + "_query"

    # Escape special regex characters in label patterns, then join with |
    # Labels may contain: @ / : _ - .
    # Of these, only / and . need escaping in regex
    escaped = [p.replace("/", "\\/").replace(".", "\\.") for p in forbidden]
    pattern = "|".join(escaped)

    native.genquery(
        name = query_name,
        expression = "filter('{}', deps({}))".format(pattern, target),
        scope = [target],
    )
    sh_test(
        name = name,
        srcs = ["//tools/deps:assert_empty.sh"],
        data = [":" + query_name],
        args = ["$(location :{})".format(query_name)],
        **kwargs
    )
