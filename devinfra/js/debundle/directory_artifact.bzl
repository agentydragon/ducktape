"""Directory inputs to debundle's Bazel rules."""

def single_directory(target):
    """Return the only output of a target that supplies a directory tree."""
    files = target[DefaultInfo].files.to_list()
    if len(files) != 1 or not files[0].is_directory:
        fail("{} must provide exactly one directory artifact".format(target.label))
    return files[0]

def named_package_roots(package_roots):
    """Index the label-to-name attribute by the package names the CLI receives."""
    roots = {}
    for target, name in package_roots.items():
        if name in roots:
            fail("package_roots contains duplicate package name {!r}".format(name))
        roots[name] = target
    return roots
