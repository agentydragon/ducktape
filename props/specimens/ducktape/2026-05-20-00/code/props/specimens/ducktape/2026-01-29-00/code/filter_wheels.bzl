"""Helper to filter wheels that require system dependencies."""

# Packages requiring native system libraries not available in Bazel sandbox.
# These packages need libraries like dbus-1, girepository-2.0, cairo which
# are not present in the hermetic Python toolchain.
# Format: normalized package names (underscores, lowercase) as they appear in @pypi//
SYSTEM_PACKAGES = [
    "dbus_python",  # requires dbus-1
    "pycairo",  # requires cairo
    "pygobject",  # requires girepository-2.0
]

def filter_system_packages(all_wheels):
    """Filter out packages that need system libraries unavailable in Bazel sandbox.

    Args:
        all_wheels: List of wheel targets from all_whl_requirements.

    Returns:
        Filtered list with system packages removed.
    """
    filtered = []
    for wheel in all_wheels:
        include = True
        for pkg in SYSTEM_PACKAGES:
            if pkg in wheel:
                include = False
                break
        if include:
            filtered.append(wheel)
    return filtered
