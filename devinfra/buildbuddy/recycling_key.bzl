"""Construct BuildBuddy runner recycling keys from retained-infrastructure inputs."""

def artifact_checksum(sha256):
    """Use the pinned checksum of a non-image artifact as a compatibility input."""
    return struct(digest = sha256)

def runner_recycling_key(family, compatibility_inputs):
    """Construct a key from a versioned family and ordered pinned inputs.

    OCI inputs are generated image pin descriptors with a digest and OCI target
    from the same source. Use artifact_checksum for pinned non-image artifacts.
    Callers own the inputs and must bump the family for incompatible changes.
    Preserve input order to keep existing keys stable.
    """
    return "-".join([family] + [pin.digest for pin in compatibility_inputs])
