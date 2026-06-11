"""Helper to filter wheels that require system dependencies."""

def filter_system_packages(all_wheels):
    """Filter out packages that need system libraries unavailable in Bazel sandbox.

    Args:
        all_wheels: List of wheel targets from all_whl_requirements.

    Returns:
        Filtered list with system packages removed.
    """
    system_packages = ["pygobject", "dbus-python", "pycairo"]
    filtered = []
    for wheel in all_wheels:
        include = True
        for pkg in system_packages:
            if pkg in wheel:
                include = False
                break
        if include:
            filtered.append(wheel)
    return filtered
