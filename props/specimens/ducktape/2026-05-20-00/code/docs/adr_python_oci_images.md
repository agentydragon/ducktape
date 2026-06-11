# ADR: Standardize Python OCI images on debian-slim + py_image_layer

## Status

Accepted (2026-02-10)

## Context

We build multiple Python OCI images in this repository. The original approach
used `pkg_tar` to bundle a `py_binary` and its runfiles into a single tar
layer, on top of `gcr.io/distroless/cc-debian12` as the base image.

`aspect_rules_py` provides `py_image_layer`, which splits a Python binary into
three cache-friendly layers:

- **interpreter** (~118 MB) — the hermetic CPython runtime
- **packages** (2-50 MB) — pip dependencies
- **default** (<1 MB) — our application code

This means incremental image rebuilds only push the layers that actually
changed. A code-only change pushes <1 MB instead of the full image.

The `aspect_rules_py` launcher for `py_binary` is a bash script that creates a
venv and `exec`s the Python interpreter. This requires `/bin/bash` in the base
image, which `distroless/cc-debian12` does not provide.

## Decision

All Python OCI images use:

- **Base image:** `@debian_slim_linux_amd64` (`docker.io/library/debian` bookworm-slim)
- **Layering:** `py_image_layer` from `@aspect_rules_py`
- **Entrypoint/env:** `py_image_entrypoint()` and `py_image_env()` helpers from `//props:oci.bzl`

## Size impact

| Component                     | Compressed size |
| ----------------------------- | --------------- |
| `distroless/cc-debian12` base | ~10.6 MB        |
| `debian:bookworm-slim` base   | ~28.2 MB        |
| **Delta**                     | **~18 MB**      |
| CPython interpreter layer     | ~118 MB         |
| Typical packages layer        | 3-50 MB         |

The ~18 MB base image increase is small relative to the ~120-170 MB total image
size and is offset by the caching benefits of the layered approach.

## Consequences

- All Python images in the repo follow one pattern — no special cases.
- Code-only changes produce sub-megabyte layer pushes.
- The interpreter layer is shared across images on the same host, reducing
  total disk usage when multiple images are deployed together.
- Images include a shell (`/bin/bash`), which is useful for debugging but
  increases the attack surface compared to distroless. For our use case
  (internal infrastructure), this is an acceptable tradeoff.
