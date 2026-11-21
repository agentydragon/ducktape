"""BuildBuddy image cache for tests using the Props container E2E fixtures."""

load("@image_pins//:pins.bzl", "POSTGRES_18_DIGEST", "REGISTRY_3_DIGEST", "RYUK_DIGEST")

# Each consumer loads all three images, so any can seed this family. Keep changing
# application images out of the key: the fixtures check their current digests.
# Bump v1 for an incompatible preparation recipe; image repins change it directly.
# The RBE platform supplies recycling. Keep remote snapshot/save policy defaults.
_IMAGE_RECIPE = "-".join([
    POSTGRES_18_DIGEST,
    REGISTRY_3_DIGEST,
    RYUK_DIGEST,
])

CONTAINER_E2E_EXEC_PROPERTIES = {
    "test.runner-recycling-key": "props-e2e-images-v1-" + _IMAGE_RECIPE,
}

# One recipe feeds the generated runtime configuration and the recycling family.
# Bump v2 for incompatible registry storage/reset changes. Budget changes and
# infrastructure repins invalidate both automatically.
_REGISTRY_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024
REGISTRY_CACHE_RECIPE = {
    "key": "props-e2e-registry-blobs-v2-" + str(_REGISTRY_CACHE_MAX_BYTES) + "-" + _IMAGE_RECIPE,
    "max_bytes": _REGISTRY_CACHE_MAX_BYTES,
}

# Remote-only permission keeps local fixture setup on its normal cold path.
REGISTRY_CACHE_E2E_EXEC_PROPERTIES = {
    "test.runner-recycling-key": REGISTRY_CACHE_RECIPE["key"],
    "test.env-overrides": "PROPS_E2E_REGISTRY_BLOB_CACHE=1",
}
