"""Execution properties for tests using the stock PostgreSQL 18 and Ryuk images."""

load("@image_pins//:pins.bzl", "POSTGRES_18_DIGEST", "RYUK_DIGEST")
load("//devinfra/buildbuddy:recycling_key.bzl", "runner_recycling_key")

# Digests invalidate the family when either image changes; bump v1 for image-only recipe changes.
POSTGRES_IMAGE_EXEC_PROPERTIES = {
    "test.runner-recycling-key": runner_recycling_key("postgres-images-v1", [
        POSTGRES_18_DIGEST,
        RYUK_DIGEST,
    ]),
}
