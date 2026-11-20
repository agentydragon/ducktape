# Docker fixture timeouts on RBE

The unresolved boundary is Docker container startup after image loading. The August timeout
measurements motivate that diagnosis, but do not establish the cause.

The clearest measurement was `//haku/console:test_operator_identity`: it timed out after
308.8s in a full run and passed alone in 27.7s ([PR #4014](https://github.com/agentydragon/ducktape/pull/4014)).
The timed-out test log stopped after a `fastapi.testclient` warning, before collection. That
shows no pytest progress; silence alone does not identify the blocked step.

Each Docker-backed Bazel target has its own pytest process and session fixture. PostgreSQL
setup loads the Ryuk and database images, then calls `container.start()`. Before the per-tag
lock, concurrent callers sharing a daemon could all see an image miss and import the same
layers at once. The current [`load_oci_image`](../../oci/oci.py) locks by image reference,
rechecks after acquiring the lock, and records the loaded layout digest with the daemon image
ID. That herd is a plausible contributor to the timeout pattern, not a confirmed explanation:
the measurements do not show that the affected CI actions shared a daemon and lock filesystem
or that the Haku timeout was spent importing an image. The [later OCI comparison](../../debug/oci_loader_benchmark/README.md)
also leaves cross-action sharing unestablished.

Docker's classic image store rewrites the OCI config during load, so the daemon's `.Id` differs
from the layout config digest. The marker must store both the layout digest and the daemon ID
observed after loading, then compare each with its current value. The loader documents this
in [`_already_loaded`](../../oci/oci.py#L150).

`start_postgres_container` writes per-step timings to `container_setup.log`, including lock
wait, image load, and container startup ([fixture](../../../testing/postgres_fixtures.py)).
The file handler flushes each record to Bazel's undeclared outputs as `container_setup.log`, so
that artifact can distinguish a slow import from time in `container.start()`. The image-load
lock does not serialize or bound container starts; that contention remains unresolved, and the
August data do not establish it as the cause of the Haku timeout.
