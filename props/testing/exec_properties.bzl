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

# The fixture retains blobs but resets manifests/uploads and starts fresh services.
# Bump this version for incompatible registry_blob_cache.py storage/reset changes.
# Remote-only permission keeps local fixture setup on its normal cold path.
REGISTRY_CACHE_E2E_EXEC_PROPERTIES = {
    "test.runner-recycling-key": "props-e2e-registry-blobs-v1-" + _IMAGE_RECIPE,
    "test.env-overrides": "PROPS_E2E_REGISTRY_BLOB_CACHE=1",
}
