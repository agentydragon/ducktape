"""Bazel rules for verifying dependency constraints."""

load("@bazel_skylib//rules:diff_test.bzl", "diff_test")

def assert_no_deps(name, targets, forbidden, **kwargs):
    """Verify that target(s) don't depend on any of the forbidden labels.

    Query matching transitive dependencies and compare the result to an empty file.

    Args:
        name: Test name (will also be used as prefix for genquery target)
        targets: List of targets to check dependencies for
        forbidden: List of Bazel label patterns to forbid (e.g., ["@pypi//mcp"])
        **kwargs: Additional arguments passed to diff_test (e.g., tags)
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

    native.genquery(
        name = query_name,
        expression = "filter('{}', deps(set({})))".format(pattern, " ".join(targets)),
        scope = targets,
    )
    diff_test(
        name = name,
        file1 = Label("//devinfra/deps:empty"),
        file2 = ":" + query_name,
        failure_message = "Unexpected dependencies found",
        **kwargs
    )
