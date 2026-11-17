"""BuildBuddy image cache for tests using the Props container E2E fixtures."""

load("@image_pins//:pins.bzl", "POSTGRES_18_PIN", "REGISTRY_3_PIN", "RYUK_PIN")
load("//devinfra/buildbuddy:recycling_key.bzl", "runner_recycling_key")

# Each consumer loads all three images, so any can seed this family. Keep changing
# application images out of the key: the fixtures check their current digests.
# Bump v1 for an incompatible preparation recipe; image repins change it directly.
# The RBE platform supplies recycling. Keep remote snapshot/save policy defaults.
CONTAINER_E2E_EXEC_PROPERTIES = {
    "test.runner-recycling-key": runner_recycling_key("props-e2e-images-v1", [
        POSTGRES_18_PIN,
        REGISTRY_3_PIN,
        RYUK_PIN,
    ]),
}
