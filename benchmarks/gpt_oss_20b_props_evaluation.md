# Running Props Evaluation with Local gpt-oss-20b

End-to-end procedure for running the props critic and grader against a committed
specimen snapshot using gpt-oss-20b served locally via llama-server (or Ollama).

## Prerequisites

- Podman daemon running (session hook auto-starts it)
- `DOCKER_HOST` pointing to the podman socket
- At least ~12 GiB RAM free for model loading
- At least ~12 GiB disk for the GGUF model file

## Step 1: Download the GGUF Model

```bash
mkdir -p /tmp/benchmark
curl -L -o /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf \
  "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf"
```

~11 GiB download.

## Step 2: Reconfigure Podman Storage (gVisor)

In gVisor, root filesystem space is limited. Move podman storage to `/dev/shm`:

```bash
# Edit /root/.cache/claude-hooks/podman/storage.conf
# Change graphroot and runroot:
#   runroot = "/dev/shm/podman-runroot"
#   graphroot = "/dev/shm/podman-storage"
mkdir -p /dev/shm/podman-storage /dev/shm/podman-runroot
```

Then restart the podman daemon (kill existing, re-launch with same socket path).

**Important**: Set `TMPDIR=/dev/shm` for all podman operations so image layer
downloads don't fill root `/tmp`.

## Step 3: Start Inference Server

### Option A: llama-server (Recommended — 24 MB download)

`llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp) provides
an OpenAI-compatible API and is only ~24 MB (CPU build). Compare this to
Ollama's 1.4 GB binary archive or 6 GB container image.

```bash
# Download and extract llama-server (CPU-only build, ~24 MB)
LLAMA_VERSION=b7993
curl -sL "https://github.com/ggml-org/llama.cpp/releases/download/${LLAMA_VERSION}/llama-${LLAMA_VERSION}-bin-ubuntu-x64.tar.gz" | \
  tar -xzf - -C /tmp/benchmark

# Start serving the GGUF model with OpenAI-compatible API
LD_LIBRARY_PATH="/tmp/benchmark/llama-${LLAMA_VERSION}" \
  /tmp/benchmark/llama-${LLAMA_VERSION}/llama-server \
    --model /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf \
    --host 127.0.0.1 --port 11434 \
    --ctx-size 4096 &
```

Verify: `curl -s http://localhost:11434/v1/models`

`llama-server` exposes `/v1/chat/completions`, `/v1/completions`, and
`/v1/models`, which is sufficient for the props upstream configuration.

### Option B: Ollama (heavier, more model management features)

Ollama wraps llama.cpp with model management, but the download is much larger:
the container image is ~6 GB and the binary archive is ~1.4 GB (includes
CUDA/ROCm libraries). Use this if you need multi-model management or
`ollama pull` convenience.

```bash
# Binary install (~1.4 GB download including GPU libraries)
apt-get install -y zstd
mkdir -p /tmp/ollama-bin
curl --fail --show-error --location --progress-bar \
  "https://ollama.com/download/ollama-linux-amd64.tar.zst" | \
  zstd -d | tar -xf - -C /tmp/ollama-bin

OLLAMA_MODELS=/tmp/benchmark/ollama-models \
  /tmp/ollama-bin/bin/ollama serve &

# Upload GGUF and create model
DIGEST=$(sha256sum /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf | cut -d' ' -f1)
curl -X POST -T /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf \
  -H "Content-Type: application/octet-stream" \
  "http://localhost:11434/api/blobs/sha256:$DIGEST"
curl -X POST http://localhost:11434/api/create \
  -d "{\"model\": \"gptoss\", \"files\": {\"gptoss\": \"sha256:$DIGEST\"}}"
```

Both options expose an OpenAI-compatible API on port 11434 that props uses via
the `ollama` upstream config.

## Step 4: Start PostgreSQL

```bash
mkdir -p /dev/shm/pgdata

podman run -d --name postgres --network=host \
  -e POSTGRES_PASSWORD=props-bench-dcfc0ef9506c6673 \
  -e POSTGRES_DB=eval_results \
  -v /dev/shm/pgdata:/var/lib/postgresql/data \
  docker.io/library/postgres:16
```

**Note**: Data is on tmpfs — it won't survive reboots. For persistence, use a
disk-backed path instead.

## Step 5: Initialize Props Database

```bash
PGHOST=127.0.0.1 \
PGPORT=5432 \
PGUSER=postgres \
PGPASSWORD="props-bench-dcfc0ef9506c6673" \
PGDATABASE=eval_results \
ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens" \
  bazel run //props/cli -- db recreate --yes
```

This creates the schema, syncs snapshots from `props/specimens/` into the DB,
and creates admin credentials. The `ADGN_PROPS_SPECIMENS_ROOT` env var must
point to the specimens directory — each subdirectory (e.g.,
`wt/2025-01-03-00/`) contains a `manifest.yaml` describing the specimen. The
`db recreate` command walks this tree and inserts snapshot rows into the
database, making them available as evaluation targets.

**Note**: No manual model metadata insert is needed. The backend syncs custom
models from the config file's `[[models]]` entries into the `model_metadata`
DB table automatically on startup (see `sync_model_metadata_with_session` in
the backend lifespan). The `gptoss` model defined in the config TOML (Step 7)
will be synced when the backend starts (Step 8).

## Step 6: Start OCI Registry (Upstream for Backend Proxy)

The props backend includes a built-in OCI registry proxy (`/v2/*` routes) that
enforces ACLs and records `agent_definitions` on push. This proxy forwards to
an upstream `registry:2` container. Images are pushed to the **backend** (port
8000), not directly to the upstream registry.

```bash
podman run -d --name registry --network=host \
  docker.io/library/registry:2
```

Verify: `curl http://localhost:5000/v2/_catalog`

The upstream registry runs on port 5000. The backend (started in Step 8)
connects to it via `PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5000`.

### Configure Insecure Registry (Required)

Podman defaults to HTTPS for registry connections. The local registry runs
plain HTTP, so you **must** configure insecure access. Without this, `podman
pull` from the local registry fails with `http: server gave HTTP response to
HTTPS client`.

Add these entries to `/root/.cache/claude-hooks/podman/registries.conf`
(after the existing `[[registry]]` for `docker.io`):

```toml
[[registry]]
location = "localhost:5000"
insecure = true

[[registry]]
location = "127.0.0.1:5000"
insecure = true
```

**Important**: The session start hook preserves this file if the
`CANARY:SESSION_START_HOOK` marker is present, but verify after session restarts.
Restart the podman daemon after editing for changes to take effect.

## Step 7: Create Props Config File

Create `/tmp/props-ollama-config.toml`. This config defines:

1. **Upstream connection** (`[upstreams.ollama]`): How to reach the local
   inference server (llama-server or Ollama on port 11434).
2. **Custom model** (`[[models]]`): Declares `gptoss` as a model routed
   through the `ollama` upstream. These entries are synced into the
   `model_metadata` DB table on backend startup (`sync_model_metadata_with_session`),
   so the model is available for cost tracking and routing. All pricing fields
   are `0.0` since inference is local and free.

```toml
backend_url = "http://127.0.0.1:8000"

[agent_env]
PGHOST = "127.0.0.1"
PGPORT = "5432"
PGDATABASE = "eval_results"

[upstreams.ollama]
url = "http://127.0.0.1:11434"
api_key_env = "OLLAMA_DUMMY_KEY"

[[models]]
name = "gptoss"
upstream = "ollama"
upstream_model = "gptoss"
input_usd_per_1m_tokens = 0.0
cached_input_usd_per_1m_tokens = 0.0
output_usd_per_1m_tokens = 0.0
context_window_tokens = 4096
max_output_tokens = 4096
```

**How model routing works**: The backend resolves model IDs to upstream
connections in two steps:

- **DB lookup**: `model_metadata` table maps `model_id` → `upstream_name` +
  `upstream_model` + pricing. Custom models from config are synced on backend
  startup; OpenAI models are synced from `openai_utils/model_metadata.yaml`
  via `db recreate`.
- **Config lookup**: `upstreams.<upstream_name>` provides the URL and API key
  env var for the upstream. The backend reads API keys from environment
  variables named by `api_key_env`.

For local models, set `api_key_env` to any env var containing a dummy value
(Ollama/llama-server don't validate API keys).

## Step 8: Start the Props Backend

```bash
PROPS_CONFIG_FILE=/tmp/props-ollama-config.toml \
OLLAMA_DUMMY_KEY=dummy \
PGHOST=127.0.0.1 \
PGPORT=5432 \
PGUSER=postgres \
PGPASSWORD="props-bench-dcfc0ef9506c6673" \
PGDATABASE=eval_results \
ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens" \
DOCKER_HOST="unix:///tmp/claude-podman-*.sock" \
PROPS_DOCKER_NETWORK=host \
PROPS_REGISTRY_UPSTREAM_URL=http://127.0.0.1:5000 \
TMPDIR=/dev/shm \
  bazel run //props/backend:backend_cli -- serve --host 127.0.0.1 --port 8000 &
```

**Critical env vars**:

- `DOCKER_HOST` — points to the podman socket
- `PROPS_DOCKER_NETWORK=host` — agent containers use host networking (required
  for podman/gVisor where bridge networking isn't available)
- `PROPS_REGISTRY_UPSTREAM_URL` — the backend's registry proxy (`/v2/*`)
  forwards to this upstream `registry:2` on port 5000
- `TMPDIR=/dev/shm` — prevents image layer downloads from filling root `/tmp`

The backend logs an admin token on startup.

## Step 9: Build and Push Critic Image (via Backend Proxy)

Push the critic image **through the backend's registry proxy** (port 8000),
not directly to the upstream registry (port 5000). The proxy records
`agent_definitions` in the DB on manifest push, which is required for the
backend to resolve `definition_id: "latest"` in run requests.

```bash
# The oci_push BUILD target is pre-configured to push to localhost:8000/critic
export TMPDIR=/dev/shm
ADMIN_TOKEN="<from backend startup logs>"
bazel run //props/agents/critic:push -- \
  --repository localhost:8000/critic --tag latest
```

The push requires HTTP Basic auth with the admin credentials (username
`admin`, password is the admin token from backend startup).

Verify the image was recorded:

```bash
curl -s http://localhost:8000/v2/critic/tags/list
```

### Pre-pull the Image in Podman

```bash
TMPDIR=/dev/shm podman pull --tls-verify=false 127.0.0.1:5000/critic:latest
```

The backend pulls images from the upstream registry (port 5000) when starting
agent containers. Pre-pulling avoids timeout issues during the first run.

## Step 10: Run a Critic

```bash
ADMIN_TOKEN="<from backend logs>"

curl -s -X POST "http://localhost:8000/api/runs/critic" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition_id": "latest",
    "example": {"kind": "whole_snapshot", "snapshot_slug": "wt/2025-01-03-00"},
    "critic_model": "gptoss",
    "timeout_seconds": 1800,
    "budget_usd": 0.0
  }'
```

`budget_usd=0.0` is required for free local models (the validation allows
`ge=0`).

## Step 11: Run a Grader

After the critic run completes, run the grader:

```bash
curl -s -X POST "http://localhost:8000/api/runs/grader" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition_id": "latest",
    "example": {"kind": "whole_snapshot", "snapshot_slug": "wt/2025-01-03-00"},
    "grader_model": "gptoss",
    "timeout_seconds": 1800,
    "budget_usd": 0.0
  }'
```

## Current Status

**In progress**: Backend + critic run. Using Ollama in this session (already
downloaded), but future runs should prefer llama-server (24 MB vs 1.4 GB).

## Troubleshooting

### `no space left on device` During Image Pull

Podman stores image layers in `TMPDIR` (defaults to `/tmp` on root fs). Set
`TMPDIR=/dev/shm` before any podman operation.

### `http: server gave HTTP response to HTTPS client`

Configure insecure registries in `registries.conf` (see Step 6).

### `requires more system memory than available`

Ollama loads model into RAM. With tmpfs, blobs also consume RAM. Use
disk-backed model storage: mount the GGUF file directly into the Ollama
container and create the model from the local file.

### `budget must be greater than 0`

Props validation required `budget_usd > 0` by default. The fix (in this branch)
changes it to `ge=0` so free local models can use `budget_usd=0.0`.

### Agent Container Can't Reach Services

Set `PROPS_DOCKER_NETWORK=host` so agent containers share the host network
namespace and can reach `127.0.0.1:*` services.
