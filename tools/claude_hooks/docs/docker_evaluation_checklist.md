# Docker Evaluation Checklist

Evaluating whether to switch from custom Podman setup to pre-installed dockerd in Claude Code container.

## Status Legend

- [ ] Not tested
- [✓] Tested - passed
- [✗] Tested - failed
- [~] Tested - partial/needs investigation
- [N/A] Not applicable

---

## 1. Basic Daemon Configuration & Startup

- [ ] **How to start dockerd in gVisor environment**
  - [ ] Test if dockerd starts under supervisord (like Podman does)
  - [ ] Check if it needs special flags for gVisor compatibility
  - [ ] Verify Unix socket creation and permissions
  - [ ] Test daemon startup time vs Podman
  - [ ] Check if daemon survives session resume/compact events

- [ ] **Default configuration analysis**
  - [ ] Does `/etc/docker/daemon.json` exist with sensible defaults?
  - [ ] What storage driver does it use by default? (overlay2, vfs, other?)
  - [ ] What runtime does it use by default? (runc, crun, other?)
  - [ ] Network backend configuration
  - [ ] Default registry configuration

- [ ] **Environment variables needed**
  - [ ] Does `DOCKER_HOST` work out of the box?
  - [ ] SSL/TLS CA bundle environment vars (compare to Podman's SSL_CERT_FILE, REQUESTS_CA_BUNDLE, etc.)
  - [ ] Proxy environment variable propagation to daemon
  - [ ] Any other env vars the daemon needs

## 2. Storage Driver & Layer Caching

- [ ] **Storage driver compatibility**
  - [ ] Does overlay2 work on tmpfs in gVisor? (Podman uses overlay on tmpfs)
  - [ ] Does it support the same ~50 layer limit or different?
  - [ ] What's the fallback storage driver if overlay2 fails? (vfs like Podman?)
  - [ ] Where does it store layers by default?
  - [ ] Can storage root be relocated to tmpfs for performance?

- [ ] **Layer caching performance**
  - [ ] Build a multi-stage Dockerfile with many layers (e.g., 20-30 layers)
  - [ ] Verify unchanged layers are cached on rebuild
  - [ ] Compare with current Podman setup
  - [ ] Test if cache survives daemon restart

## 3. Build Functionality (docker build / buildkit)

- [ ] **Basic build**
  - [ ] `docker build -t test .` with a simple Dockerfile (FROM, RUN, COPY)
  - [ ] Multi-stage builds
  - [ ] Build arguments and env vars
  - [ ] SHELL directive support (Podman requires Docker image format for this)

- [ ] **BuildKit vs classic builder**
  - [ ] Does the container use BuildKit by default? (`DOCKER_BUILDKIT=1`)
  - [ ] Does classic builder work if BuildKit is disabled?
  - [ ] Which one handles gVisor better?
  - [ ] BuildKit cache mount support (`RUN --mount=type=cache`)

- [ ] **Large output handling**
  - [ ] Test RUN commands with large stdout (Podman has SIGPIPE bug >3MB)
  - [ ] `RUN seq 1 1000000` - does it complete or fail with SIGPIPE?
  - [ ] Does BuildKit handle this better than classic builder?

- [ ] **Layer limit**
  - [ ] Build a Dockerfile with 60+ RUN steps
  - [ ] Does it hit mount option size limits like Podman (~50 layers)?
  - [ ] Does `--squash` or BuildKit help?

## 4. gVisor-Specific Workarounds

**Current Podman setup has custom workarounds for:**

- [ ] **`/proc/self/setgroups` missing**
  - [ ] Test if runc/crun fail with "No such file or directory" for setgroups
  - [ ] Does Docker's default runc handle this gracefully?
  - [ ] Do we need a runc-gvisor-wrapper like crun-gvisor-wrapper?
  - [ ] Test with: `docker run --rm alpine id`

- [ ] **Kernel keyring quota (60-70 limit)**
  - [ ] Build a Dockerfile with 100+ RUN steps
  - [ ] Does it exhaust keyring quota and fail?
  - [ ] Does Docker/runc support `--no-new-keyring` flag?
  - [ ] Can we configure this in daemon.json or runtime config?

- [ ] **Cgroup freezer for `docker exec`**
  - [ ] Does `docker exec -it <container> sh` work?
  - [ ] Check if runc needs cgroup v1 freezer like crun does
  - [ ] Test: `docker run -d --name test alpine sleep 3600 && docker exec test echo hello`

- [ ] **Host user namespace requirement**
  - [ ] Does Docker default to `userns=host`?
  - [ ] Can it be configured in daemon.json?
  - [ ] Test container user mapping behavior

- [ ] **OCI vs chroot isolation**
  - [ ] Does Docker use OCI isolation by default?
  - [ ] Podman needs `BUILDAH_ISOLATION=oci` to avoid read-only /dev/null
  - [ ] Test if /dev/null is writable in builds

## 5. Networking

- [ ] **Host networking requirement**
  - [ ] Does Docker support `--network=host` in gVisor?
  - [ ] What's the default network mode?
  - [ ] Can daemon.json force host networking as default?
  - [ ] Test: `docker run --network=host --rm alpine ping -c1 8.8.8.8`

- [ ] **Bridge networking**
  - [ ] Does bridge networking work in gVisor? (Podman can't do this)
  - [ ] If it works, does it offer any advantages?
  - [ ] If it doesn't, document the limitation

## 6. Runtime Configuration

- [ ] **OCI runtime selection**
  - [ ] Can we use crun instead of runc? (Podman uses crun)
  - [ ] How to configure in daemon.json: `"default-runtime": "crun"`?
  - [ ] Does crun need the same gvisor-wrapper as Podman?
  - [ ] Test both runc and crun, compare compatibility

- [ ] **Runtime wrapper integration**
  - [ ] If we need a wrapper (for setgroups/keyring/freezer fixes), how to integrate?
  - [ ] Can we add custom runtimes to daemon.json `"runtimes"` section?
  - [ ] Test wrapper injection similar to Podman's crun-gvisor-wrapper

## 7. Registry & Image Pulling

- [ ] **Registry configuration**
  - [ ] Does Docker support short names (e.g., `alpine` vs `docker.io/library/alpine`)?
  - [ ] Where to configure registry mirrors / search order?
  - [ ] Test: `docker pull alpine` (without fully qualified name)

- [ ] **Proxy for image pulls**
  - [ ] Does dockerd respect http_proxy/https_proxy env vars?
  - [ ] Does it trust the custom CA bundle for TLS-inspecting proxy?
  - [ ] Test pulling an image through the proxy

- [ ] **Image signature policy**
  - [ ] Podman uses `~/.config/containers/policy.json` (insecureAcceptAnything)
  - [ ] Does Docker need similar configuration?
  - [ ] Or does it accept unsigned images by default?

## 8. Compatibility & Migration

- [ ] **Image format compatibility**
  - [ ] Can Docker load images built by Podman?
  - [ ] Can Podman load images built by Docker?
  - [ ] Test: `podman save` → `docker load` and vice versa

- [ ] **Dockerfile compatibility**
  - [ ] Do Dockerfiles that work with `podman build` work with `docker build`?
  - [ ] Test your existing Dockerfiles (e.g., RBE worker image)

- [ ] **CLI compatibility**
  - [ ] Are flags the same? (e.g., `--layers=false` in Podman)
  - [ ] BuildKit has different flags than classic builder

## 9. Performance & Resource Usage

- [ ] **Daemon resource footprint**
  - [ ] Memory usage: dockerd vs podman system service
  - [ ] CPU usage during idle and during builds
  - [ ] Storage space for daemon internals

- [ ] **Build performance**
  - [ ] Time to build a complex multi-stage Dockerfile
  - [ ] Compare with current Podman setup
  - [ ] Test with and without BuildKit

- [ ] **Startup time**
  - [ ] How long does dockerd take to start?
  - [ ] Faster/slower than Podman service startup?

## 10. Daemon Management & Reliability

- [ ] **Process supervision**
  - [ ] Integrate dockerd with supervisord (like Podman)
  - [ ] Test auto-restart on crash
  - [ ] Check log output location and rotation

- [ ] **Health checks**
  - [ ] How to verify daemon is healthy? (`docker info`?)
  - [ ] Socket availability check
  - [ ] Test recovery from temporary failures

- [ ] **Cleanup & garbage collection**
  - [ ] Does Docker have automatic garbage collection?
  - [ ] How to clean up old images/layers? (`docker system prune`)
  - [ ] Does cleanup work correctly in gVisor?

## 11. Special Features

- [ ] **BuildKit cache mounts**
  - [ ] `RUN --mount=type=cache,target=/root/.cache` support
  - [ ] Does it work in gVisor?
  - [ ] Performance benefits vs layer caching

- [ ] **Multi-platform builds**
  - [ ] Does `docker buildx` work in gVisor?
  - [ ] Needed for cross-compilation?

- [ ] **Volume mounts in builds**
  - [ ] Can COPY use bind mounts as BuildKit cache?
  - [ ] Test: Build with large cached dependencies

## 12. Edge Cases & Known Issues

- [ ] **Specific gVisor limitations**
  - [ ] Review gVisor documentation for Docker incompatibilities
  - [ ] Test scenarios that failed with Podman
  - [ ] Document any new limitations found

- [ ] **SIGPIPE on large RUN output**
  - [ ] Does Docker/BuildKit have the same bug as Podman/buildah?
  - [ ] Test tolerance level (Podman fails at ~440k lines)

- [ ] **Concurrent builds**
  - [ ] Can dockerd handle multiple builds in parallel?
  - [ ] Resource limits per build

## 13. Configuration Persistence

- [ ] **Where to store daemon.json**
  - [ ] Does it need to be in `/etc/docker/`?
  - [ ] Can we use a custom location like Podman's `~/.cache/claude-hooks/docker/`?
  - [ ] Test with `dockerd --config-file=<path>`

- [ ] **Configuration generation**
  - [ ] Write equivalent of `setup_podman_storage()` for Docker
  - [ ] Template for daemon.json with all necessary gVisor workarounds
  - [ ] How to detect and handle config drift

## 14. Documentation & Debugging

- [ ] **Error messages**
  - [ ] Are Docker error messages clearer than Podman's?
  - [ ] Do they help with debugging gVisor-specific issues?

- [ ] **Logging**
  - [ ] Where does dockerd write logs?
  - [ ] Can we redirect to supervisord stdout?
  - [ ] Log verbosity controls

## 15. Re-evaluating Current Podman Custom Configurations

**For each custom configuration in the current Podman setup, verify if it's actually necessary with Docker:**

### Storage Configuration

- [ ] **Overlay on tmpfs**
  - [ ] Current: Podman explicitly uses tmpfs for overlay storage (`/mnt/bazel-tmpfs/podman-overlay`)
  - [ ] Question: Does Docker's default overlay2 already use tmpfs in this environment?
  - [ ] Question: Does Docker's overlay2 perform well on 9p filesystem without tmpfs?
  - [ ] Test: Run Docker with default storage, measure build performance vs tmpfs
  - [ ] Conclusion: Is explicit tmpfs configuration necessary or just cargo-cult?

- [ ] **VFS fallback**
  - [ ] Current: Podman falls back to VFS when tmpfs unavailable
  - [ ] Question: Does Docker need explicit VFS configuration or auto-detects?
  - [ ] Question: Is VFS performance acceptable if we don't have tmpfs?
  - [ ] Test: Disable tmpfs, see what storage driver Docker chooses

- [ ] **Isolated storage paths**
  - [ ] Current: Podman uses `~/.cache/claude-hooks/podman/storage.conf` to avoid system conflicts
  - [ ] Question: Do we have a "system" Docker that could conflict?
  - [ ] Question: Can we just use Docker's default `/var/lib/docker`?
  - [ ] Test: Check if default paths work or cause permission/conflict issues

### User Namespace Configuration

- [ ] **`userns = "host"`**
  - [ ] Current: Podman requires host user namespace for gVisor
  - [ ] Question: Does Docker default to host user namespace?
  - [ ] Question: Does gVisor prevent user namespace creation for Docker too?
  - [ ] Test: Run `docker run --rm alpine id` without any config
  - [ ] Test: Try removing userns config and see if Docker auto-detects the limitation
  - [ ] Conclusion: Is this configuration necessary or does Docker handle it?

### Network Configuration

- [ ] **`netns = "host"`**
  - [ ] Current: Podman forces host networking because gVisor doesn't support bridge
  - [ ] Question: Does Docker bridge networking actually work in gVisor?
  - [ ] Question: If bridge doesn't work, does Docker fail gracefully or need explicit config?
  - [ ] Test: `docker run --rm alpine ping -c1 8.8.8.8` (default network)
  - [ ] Test: `docker run --network=bridge --rm alpine ping -c1 8.8.8.8`
  - [ ] Conclusion: Can we skip netns config and let Docker handle it?

### OCI Annotations

- [ ] **`run.oci.keep_original_groups=1`**
  - [ ] Current: Podman sets this globally because `/proc/self/setgroups` is missing
  - [ ] Question: Does runc handle missing setgroups more gracefully than crun?
  - [ ] Question: Does Docker's default runc already have a workaround?
  - [ ] Test: Run containers without the annotation and check for setgroups errors
  - [ ] Conclusion: Is this workaround still needed with Docker/runc?

### Runtime Selection & Wrapper

- [ ] **Custom `crun-gvisor` runtime wrapper**
  - [ ] Current: 200+ line Python wrapper around crun
  - [ ] Question: Does runc need the same workarounds as crun?
  - [ ] Question: Can we use stock runc without any wrapper?
  - [ ] Test each wrapper fix independently:
    - [ ] **setgroups**: Does runc fail without `keep_original_groups=1`?
    - [ ] **keyring**: Does runc exhaust keyring quota without `--no-new-keyring`?
    - [ ] **freezer**: Does `docker exec` fail without mock `freezer.state`?
  - [ ] Conclusion: Which (if any) wrapper fixes are actually needed?

- [ ] **Choosing crun vs runc**
  - [ ] Current: Podman uses crun
  - [ ] Question: Why did we choose crun? Was it necessary or arbitrary?
  - [ ] Question: Does runc work better in gVisor?
  - [ ] Test: Compare build success rate and performance between runc and crun
  - [ ] Conclusion: Should we use Docker's default runc or configure crun?

### Image Format

- [ ] **`image_default_format = "docker"`**
  - [ ] Current: Podman uses Docker manifest format for SHELL directive
  - [ ] Question: Does Docker always use Docker format (obviously)?
  - [ ] Question: Was this only needed for Podman/buildah OCI vs Docker format?
  - [ ] Conclusion: Can obviously skip this with Docker

### Buildah Isolation Mode

- [ ] **`BUILDAH_ISOLATION=oci`**
  - [ ] Current: Set to avoid chroot mode's read-only /dev/null
  - [ ] Question: Is this Buildah-specific? (Yes, probably)
  - [ ] Question: Does Docker build (or BuildKit) have isolation modes?
  - [ ] Question: Does Docker suffer from read-only /dev/null issue?
  - [ ] Test: Build a Dockerfile that writes to /dev/null
  - [ ] Conclusion: Not applicable to Docker?

### Registry Configuration

- [ ] **Short name support (`unqualified-search-registries`)**
  - [ ] Current: Podman needs explicit config to pull `alpine` instead of `docker.io/library/alpine`
  - [ ] Question: Does Docker support short names by default?
  - [ ] Test: `docker pull alpine` (no fully qualified name)
  - [ ] Conclusion: Can we skip registry config entirely?

- [ ] **Registry mirrors**
  - [ ] Current: Not configured in Podman
  - [ ] Question: Would registry mirrors help with the TLS-inspecting proxy?
  - [ ] Question: Does Docker have better default registry handling?

### Image Signature Policy

- [ ] **`policy.json` with `insecureAcceptAnything`**
  - [ ] Current: Podman requires explicit policy to accept unsigned images
  - [ ] Question: Does Docker require image signature policies?
  - [ ] Question: Does Docker accept unsigned images by default?
  - [ ] Test: Pull and run unsigned images without any policy config
  - [ ] Conclusion: Is policy.json a Podman-specific requirement?

### Proxy & SSL Configuration

- [ ] **Proxy environment variable propagation**
  - [ ] Current: Podman daemon env needs explicit http_proxy/https_proxy/no_proxy
  - [ ] Question: Does dockerd inherit these from parent environment automatically?
  - [ ] Question: Does dockerd respect these for image pulls?
  - [ ] Test: Start dockerd without explicit proxy env, try pulling an image

- [ ] **SSL CA bundle configuration**
  - [ ] Current: Podman daemon needs SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE, NODE_EXTRA_CA_CERTS
  - [ ] Question: Does dockerd need all four CA env vars or just one?
  - [ ] Question: Does dockerd have a daemon.json option for CA bundle instead?
  - [ ] Test: Pull image through TLS-inspecting proxy with minimal CA config
  - [ ] Conclusion: Can we simplify CA bundle configuration?

### Supervisor Integration

- [ ] **Running under supervisord**
  - [ ] Current: Podman runs as supervised process for auto-restart
  - [ ] Question: Does dockerd have built-in daemon management?
  - [ ] Question: Can dockerd run as a systemd service even without systemd init?
  - [ ] Question: Is supervisord integration necessary or just convenient?
  - [ ] Test: Run dockerd directly vs under supervisord, compare reliability

### Layer Limit Workarounds

- [ ] **`--layers=false` recommendation for large Dockerfiles**
  - [ ] Current: Podman hits ~50 layer limit due to mount option page size
  - [ ] Question: Does Docker have the same layer limit?
  - [ ] Question: Does BuildKit handle layers differently?
  - [ ] Test: Build Dockerfile with 100+ RUN steps
  - [ ] Conclusion: Is layer limit a kernel/overlay issue or Podman-specific?

### SIGPIPE Workaround Documentation

- [ ] **Large RUN output redirection**
  - [ ] Current: Documented workaround for Buildah's SIGPIPE bug (>3MB stdout)
  - [ ] Question: Does Docker's classic builder have the same bug?
  - [ ] Question: Does BuildKit have this bug?
  - [ ] Test: `RUN seq 1 1000000` without any redirection
  - [ ] Test: Even larger output (5MB, 10MB)
  - [ ] Conclusion: Is this workaround needed with Docker?

### Isolated Config Directory Pattern

- [ ] **`~/.cache/claude-hooks/podman/` for all configs**
  - [ ] Current: Podman uses isolated directory to avoid system conflicts
  - [ ] Question: Do we need isolation for Docker?
  - [ ] Question: Can we use `/etc/docker/` and `/var/lib/docker/`?
  - [ ] Question: Are there permission issues with default paths?
  - [ ] Conclusion: Is isolation actually solving a problem or adding complexity?

### Tmpfs Mount Setup

- [ ] **Dedicated tmpfs setup (`tmpfs_setup.py`)**
  - [ ] Current: Session hook mounts tmpfs at `/mnt/bazel-tmpfs` for Podman and Bazel
  - [ ] Question: Does Docker benefit from this tmpfs?
  - [ ] Question: Is the tmpfs large enough for Docker's needs?
  - [ ] Question: Could Docker use a different tmpfs path?
  - [ ] Test: Docker with and without tmpfs, measure performance difference
  - [ ] Conclusion: Keep tmpfs setup or let Docker use default storage?

## 16. Configuration Minimalism Test

**The ultimate test: Can we run Docker with ZERO custom configuration?**

- [ ] **Completely stock Docker test**
  - [ ] Start: `dockerd` with no flags, no daemon.json, no env vars
  - [ ] Test: Build a representative Dockerfile (e.g., your RBE worker image)
  - [ ] Test: Run a container with exec
  - [ ] Test: Multi-stage build with caching
  - [ ] Document every failure and the minimal config needed to fix it
  - [ ] Goal: Identify the true minimum configuration required

- [ ] **Progressive configuration test**
  - [ ] Start with zero config (stock Docker)
  - [ ] Add configurations one at a time, only when a test fails
  - [ ] For each addition, document:
    - What failed without it?
    - What's the minimal fix?
    - Is there an alternative fix?
  - [ ] Result: Minimal viable configuration for Docker in this environment

---

## Testing Progress

**Started:** 2026-02-16
**Last Updated:** 2026-02-16

### Summary

- Total items: TBD
- Tested: 0
- Passed: 0
- Failed: 0
- Needs investigation: 0
