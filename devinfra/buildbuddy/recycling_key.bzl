"""Construct BuildBuddy runner recycling keys from retained-infrastructure inputs."""

def runner_recycling_key(family, compatibility_inputs):
    """Keep the versioned family and its ordered compatibility inputs in one key.

    Callers own which inputs define compatibility and must bump the family version
    for incompatible recipe changes. Preserve input order to keep keys stable.
    """
    return "-".join([family] + compatibility_inputs)
