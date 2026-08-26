# Cluster container images

Build contexts for container images the cluster runs that **Bazel cannot build** —
Dockerfiles driven by a GitHub Actions workflow, typically compiling third-party
upstream source at a pinned revision. One directory per image, named after the
image it produces.

Publishing, tagging, and Flux image automation are the same for every image in
the repo: <../docs/container-images.md>.

Not here:

- **Bazel-built images** (`oci_image` + an entry in <../../devinfra/ci/image_targets.json>)
  — those live in the package that owns the code.
- **Bazel external-dependency overlays** (`BUILD.bazel` fragments, `MODULE.bazel`
  fragments, patches) — <../../third_party/>, which is Bazel machinery only and
  holds no Dockerfiles.
- **Our own service source** — its own package (e.g. <../proxies/loki_read_proxy/>).
