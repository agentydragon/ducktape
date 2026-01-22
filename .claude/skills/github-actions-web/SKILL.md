---
name: github-actions-web
description: Run GitHub Actions locally in Claude Code on the web's gVisor container using act with podman. Includes workarounds for sandbox limitations.
---

# GitHub Actions in Claude Code on the Web

This skill explains how to run GitHub Actions locally in Claude Code on the web's container environment. Helper scripts auto-detect CA bundles and proxy settings.

## Quick Start

```bash
SKILL_DIR=/home/user/ducktape/.claude/skills/github-actions-web

# One-time setup (configures podman, installs act)
python3 $SKILL_DIR/setup_podman.py

# Pull the runner image
podman --log-level=error pull docker.io/catthehacker/ubuntu:act-latest

# Build custom image with global-agent for full Node.js proxy support (recommended)
cd $SKILL_DIR
cp /root/.cache/bazel-proxy/combined_ca.pem ca-bundle.pem
podman build --network=host \
  --build-arg HTTP_PROXY="$HTTP_PROXY" \
  --build-arg HTTPS_PROXY="$HTTPS_PROXY" \
  -t act-proxy:latest -f Dockerfile.act-proxy .

# List available jobs
python3 $SKILL_DIR/run_act.py -l

# Run a specific job
python3 $SKILL_DIR/run_act.py pre-commit
```

## Why These Workarounds?

Claude Code on the web runs in a **gVisor sandbox** with:

1. **No overlay filesystem** - Standard Docker/Podman storage drivers fail
2. **No DNS** - `/etc/resolv.conf` is empty; all traffic must go through proxy
3. **TLS-inspecting proxy** - All HTTPS traffic goes through Anthropic's proxy
4. **Network restrictions** - Container networking (netavark) doesn't work

## Helper Scripts

### `setup_podman.py`

Configures podman with vfs storage driver, starts the podman service, auto-detects and copies the CA bundle, and installs act.

**Auto-detected CA bundle locations:**

- `/root/.cache/bazel-proxy/combined_ca.pem`
- `/etc/ssl/certs/ca-certificates.crt`
- `$SSL_CERT_FILE`
- `$REQUESTS_CA_BUNDLE`

### `run_act.py`

Runs act with all necessary workarounds. Auto-detects:

- CA bundle location
- Proxy environment variables (HTTP_PROXY, HTTPS_PROXY, etc.)
- Custom `act-proxy:latest` image (uses if available, with `--pull=false`)

Features:

- Sets DOCKER_HOST to podman socket
- Passes all proxy environment variables
- Mounts CA bundle for TLS verification
- Uses `--network=host` to bypass netavark
- Uses `--container-options -v` for reliable volume mounts

Usage:

```bash
python3 run_act.py JOB_NAME [extra-act-args...]
python3 run_act.py -l  # List jobs
```

### `Dockerfile.act-proxy`

Custom image with `global-agent` pre-installed for full Node.js proxy support. This makes all Node.js-based GitHub Actions work with the proxy.

## What Works

With the standard `catthehacker/ubuntu:act-latest` image:

- Container startup and shell commands
- Git clone/checkout operations
- setup-python action (downloads Python from GitHub)
- pip install with proxy
- curl/wget with `--proxy` and `--cacert`

With the custom `act-proxy:latest` image (recommended):

- All of the above, plus:
- nix-installer-action (installs Nix successfully)
- Other Node.js-based actions that don't respect HTTP_PROXY
- Any action using Node.js native https module

## What May Fail (without custom image)

Some Node.js-based actions don't respect `HTTP_PROXY` (e.g., nix-installer-action). Without the custom image, these fail with `EAI_AGAIN` DNS errors.

**Solution:** Build and use the custom `act-proxy:latest` image which has `global-agent` pre-configured to route all Node.js HTTPS through the proxy.

Manual workaround (inside container):

```bash
# Install global-agent
export npm_config_proxy="$HTTP_PROXY"
export npm_config_https_proxy="$HTTPS_PROXY"
export npm_config_cafile="/tmp/ca-bundle.pem"
npm install -g global-agent

# Enable for all Node.js processes
export NODE_PATH=$(npm root -g)
export NODE_OPTIONS="-r global-agent/bootstrap"
export GLOBAL_AGENT_HTTP_PROXY="$HTTP_PROXY"
export GLOBAL_AGENT_HTTPS_PROXY="$HTTPS_PROXY"
```

## Troubleshooting

| Error                       | Solution                                          |
| --------------------------- | ------------------------------------------------- |
| `overlay: mount failed`     | Re-run `setup_podman.py` (configures vfs)         |
| `unable to find user root`  | Add root to subuid/subgid (done by setup script)  |
| `EAI_AGAIN` (DNS fails)     | Build and use `act-proxy:latest` image            |
| `self-signed certificate`   | Ensure CA bundle is mounted (done by run_act.py)  |
| `netavark: invalid version` | Use `--network=host` (done by run_act.py)         |
| Volume lock errors          | Run `podman rm --all --force`                     |
| `denied: requested access`  | Use `localhost/act-proxy:latest` for local images |

## CI Workflow Jobs

See `.github/workflows/ci.yml` for the authoritative list of jobs.

## Debugging

```bash
# Verbose output
python3 run_act.py JOB_NAME --verbose

# Very verbose
python3 run_act.py JOB_NAME -vv

# Keep containers running after failure (for debugging)
python3 run_act.py JOB_NAME --reuse
```

## Alternative: Direct Testing

For simpler cases, bypass act entirely:

```bash
# Run pre-commit directly
pip install pre-commit==4.0.1
pre-commit run --all-files

# Run Bazel directly
bazel build //...
bazel test //...
```
