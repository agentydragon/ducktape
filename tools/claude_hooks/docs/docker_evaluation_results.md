# Docker Evaluation Results

**Date:** 2026-02-17
**Environment:** Claude Code container with gVisor sandbox

## Executive Summary

✅ **Docker (dockerd) works excellently in gVisor with minimal configuration**

**Key Finding:** 90% of Podman's complex workarounds are NOT needed with Docker:

- ❌ No crun-gvisor-wrapper (200+ lines of Python)
- ❌ No setgroups annotation
- ❌ No mock freezer.state
- ❌ No --no-new-keyring injection
- ❌ No registry configuration
- ❌ No image signature policy

**Bottom Line:** Docker is simpler, more robust, and handles gVisor limitations better than Podman.

### Minimal Working Configuration

```json
{
  "iptables": false,
  "ip6tables": false,
  "data-root": "/mnt/bazel-tmpfs/docker",
  "bridge": "none"
}
```

### Build Command Required

```bash
docker build --network=host -t image-name .
```

## Detailed Findings

### ✅ Works Out of the Box (No Workarounds Needed)

1. **setgroups issue** - runc handles missing `/proc/self/setgroups` gracefully
   - No annotation needed
   - No runtime wrapper needed
   - Test: `docker run --rm alpine id` - PASSED

2. **docker exec** - Works without cgroup freezer workarounds
   - No mock `freezer.state` needed
   - Test: `docker exec <container> echo hello` - PASSED

3. **SIGPIPE on large output** - BuildKit handles this better than buildah
   - Output just gets clipped at 200KiB/s
   - Test: `RUN seq 1 1000000` - PASSED (Podman fails ~440k lines)

4. **Short image names** - Works by default
   - No registry configuration needed
   - Test: `docker pull alpine` - PASSED (no need for docker.io/library/alpine)

5. **Image signature policy** - Not required
   - No policy.json needed
   - Docker accepts unsigned images by default

6. **SSL CA bundle** - Works through environment
   - Inherited from parent process
   - Pulled images through TLS-inspecting proxy successfully

### ⚠️ Requires Configuration

1. **iptables/bridge networking** - Must be disabled
   - gVisor doesn't support iptables/nftables
   - Error without config: `iptables: Failed to initialize nft: Protocol not supported`
   - Fix: `"iptables": false, "ip6tables": false, "bridge": "none"`

2. **Storage location** - Must use tmpfs
   - Default `/var/lib/docker` is on 9p filesystem
   - 9p doesn't support overlay mounts
   - Error without tmpfs: `mount source: "overlay"... err: invalid argument`
   - Fix: `"data-root": "/mnt/bazel-tmpfs/docker"`

3. **Build networking** - Must use host network
   - Default bridge network doesn't exist (we disabled it)
   - Error: `network bridge not found`
   - Fix: `docker build --network=host ...`

### ❌ Known Limitations (Same as Podman)

1. **Layer limit** - Hit at ~35 layers (Podman ~50)
   - Kernel mount option page size limit (4096 bytes)
   - Each layer adds ~70 chars to `lowerdir` mount option
   - Error at layer 36: `mount source: "overlay"... lowerdir=59/fs:58/fs:..., err: invalid argument`
   - Workaround: Would need `--squash` or similar (needs testing)

2. **Bridge networking** - Not available in gVisor
   - Only host networking works
   - All containers share host network namespace

### 🔍 Needs Investigation (RESOLVED)

1. **~~DNS in build containers~~** - SOLVED
   - Initial error: `DNS: transient error (try again later)`
   - Root cause: TLS certificate trust issue, not DNS
   - The TLS-inspecting egress proxy injects its own certificate
   - Alpine's apk reports "DNS error" when TLS validation fails (misleading)
   - Solution: Use `apk --no-check-certificate` for builds
   - Test: `RUN apk --no-check-certificate add curl` - PASSED
   - Proxy environment variables work: `http_proxy`, `https_proxy` are respected
   - For production: Would inject combined CA bundle into base images

2. **Keyring quota**
   - Haven't tested 100+ layers yet (hit overlay limit first at 35)
   - dockerd logs show: `unable to modify root key limit`
   - Unclear if this will cause issues in practice

3. **BuildKit vs classic builder**
   - Only tested BuildKit (seems to be default)
   - Should test classic builder (`DOCKER_BUILDKIT=0`)

4. **Layer caching**
   - Basic caching seems to work (saw "CACHED" in output)
   - Should test rebuild performance

## Workarounds NOT Needed with Docker

These are all required for Podman but NOT for Docker:

1. ❌ `run.oci.keep_original_groups=1` annotation
2. ❌ crun-gvisor-wrapper script (200+ lines)
3. ❌ Mock `freezer.state` for exec
4. ❌ `--no-new-keyring` flag injection
5. ❌ `BUILDAH_ISOLATION=oci` environment variable
6. ❌ `image_default_format = "docker"` (obviously)
7. ❌ Registry configuration (`unqualified-search-registries`)
8. ❌ Image signature policy (`policy.json`)
9. ❌ `userns = "host"` (runc seems to handle this)

## Comparison: Podman vs Docker

| Feature                | Podman                 | Docker              | Winner |
| ---------------------- | ---------------------- | ------------------- | ------ |
| **Config complexity**  | High (8+ files)        | Low (1 file)        | Docker |
| **Runtime wrapper**    | Required (crun-gvisor) | Not needed          | Docker |
| **setgroups handling** | Needs annotation       | Works automatically | Docker |
| **exec support**       | Needs freezer mock     | Works automatically | Docker |
| **SIGPIPE issue**      | Fails >440k lines      | Clips at 200KiB/s   | Docker |
| **Layer limit**        | ~50 layers             | ~35 layers          | Podman |
| **Short names**        | Needs config           | Works by default    | Docker |
| **Bridge networking**  | Disabled               | Disabled            | Tie    |
| **Storage on tmpfs**   | Required               | Required            | Tie    |

## Recommendations

### Switch to Docker: YES

Docker is simpler and more robust in gVisor:

- **90% less configuration** (1 file vs 8+ files for Podman)
- **No runtime wrapper** needed (saves 200+ lines of Python)
- **Better error handling** for gVisor limitations
- **Better build experience** (BuildKit handles large output)

### Implementation Status

✅ **COMPLETE** - Docker support implemented in `docker_service.py` (196 lines)

- 70% less code than Podman (196 lines vs 600+ lines)
- Runtime configurable via `DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME` (podman/docker/none)
- Default: **Docker** (switched from Podman for better gVisor compatibility)

## Open Questions

1. ~~How to handle DNS in build containers?~~ - SOLVED: Proxy env vars work, TLS cert trust is the issue
2. ~~Do we need tmpfs?~~ - YES: overlay requires tmpfs (9p doesn't support overlay mounts)
3. What's the actual keyring limit impact? (Haven't hit it yet)
4. Can BuildKit's experimental features help with layer limits?
