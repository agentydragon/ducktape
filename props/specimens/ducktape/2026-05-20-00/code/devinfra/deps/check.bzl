"""Bazel rules for verifying dependency constraints."""

load("@rules_shell//shell:sh_test.bzl", "sh_test")

def assert_no_deps(name, targets, forbidden, **kwargs):
    """Verify that target(s) don't depend on any of the forbidden labels.

    Creates a genquery + sh_test pair that fails if any target has transitive
    dependencies matching any of the forbidden label patterns.

    Args:
        name: Test name (will also be used as prefix for genquery target)
        targets: List of targets to check dependencies for
        forbidden: List of Bazel label patterns to forbid (e.g., ["@pypi//mcp"])
        **kwargs: Additional arguments passed to sh_test (e.g., tags)
    """
    if type(targets) != "list" or len(targets) == 0:
        fail("targets must be a non-empty list, got: " + str(targets))
    if type(forbidden) != "list":
        fail("forbidden must be a list of label patterns, got: " + str(type(forbidden)))

    query_name = name + "_query"

    # Escape special regex characters in label patterns, then join with |
    # Labels may contain: @ / : _ - .
    # Of these, only / and . need escaping in regex
    escaped = [p.replace("/", "\\/").replace(".", "\\.") for p in forbidden]
    pattern = "|".join(escaped)

    # Use set() in query to union multiple targets
    if len(targets) == 1:
        target_expr = targets[0]
    else:
        target_expr = "set({})".format(" ".join(targets))

    native.genquery(
        name = query_name,
        expression = "filter('{}', deps({}))".format(pattern, target_expr),
        scope = targets,
    )
    sh_test(
        name = name,
        srcs = ["//devinfra/deps:assert_empty.sh"],
        data = [":" + query_name],
        args = ["$(location :{})".format(query_name)],
        **kwargs
    )
