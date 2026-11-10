"""BuildBuddy image cache for tests using the Props container E2E fixtures."""

load("@image_pins//:pins.bzl", "POSTGRES_18_DIGEST", "REGISTRY_3_DIGEST", "RYUK_DIGEST")

# Each consumer loads all three images, so any can seed this family. Keep changing
# application images out of the key: the fixtures check their current digests.
# Bump v1 for an incompatible preparation recipe; image repins change it directly.
# The RBE platform supplies recycling. Keep remote snapshot/save policy defaults.
CONTAINER_E2E_EXEC_PROPERTIES = {
    "test.runner-recycling-key": "props-e2e-images-v1-" + "-".join([
        POSTGRES_18_DIGEST,
        REGISTRY_3_DIGEST,
        RYUK_DIGEST,
    ]),
}
