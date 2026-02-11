# Running Props Evaluation with Local gpt-oss-20b via Ollama

End-to-end procedure for running the props critic and grader against a committed
specimen snapshot using gpt-oss-20b served locally via Ollama.

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

## Step 3: Start Ollama Container

```bash
export CONTAINER_HOST="unix:///tmp/claude-podman-*.sock"
export TMPDIR=/dev/shm

podman run -d --name ollama --network=host \
  -v /tmp/benchmark:/models:ro \
  docker.io/ollama/ollama:latest
```

### Create the gptoss Model in Ollama

1. Compute the GGUF SHA256:

   ```bash
   sha256sum /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf
   ```

2. Upload the blob:

   ```bash
   curl -T /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf \
     "http://localhost:11434/api/blobs/sha256:$DIGEST"
   ```

3. Create model via files API:

   ```bash
   curl -X POST http://localhost:11434/api/create -d '{
     "model": "gptoss",
     "files": {"gptoss": "sha256:$DIGEST"}
   }'
   ```

4. Verify: `curl http://localhost:11434/api/tags`

Ollama supports the OpenAI Responses API (`/v1/responses`) since v0.13.3, which
is what props uses.

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
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres
export PGPASSWORD="props-bench-dcfc0ef9506c6673"
export PGDATABASE=eval_results
export ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens"

bazel run //props/cli -- db recreate --yes
```

This creates the schema, inserts snapshots (from specimens dir), and creates
admin credentials.

### Insert gptoss Model Config

```sql
PGPASSWORD="props-bench-dcfc0ef9506c6673" psql -h 127.0.0.1 -U postgres -d eval_results -c "
INSERT INTO models (name, upstream, upstream_model, input_usd_per_1m_tokens,
  cached_input_usd_per_1m_tokens, output_usd_per_1m_tokens,
  context_window_tokens, max_output_tokens)
VALUES ('gptoss', 'ollama', 'gptoss', 0, 0, 0, 4096, 4096)
ON CONFLICT (name) DO NOTHING;
"
```

## Step 6: Start OCI Registry

The props system pulls agent images from an OCI registry.

```bash
podman run -d --name registry --network=host \
  docker.io/library/registry:2
```

Verify: `curl http://localhost:5000/v2/_catalog`

### Configure Insecure Registry

Add to `/root/.cache/claude-hooks/podman/registries.conf`:

```toml
[[registry]]
location = "localhost:5000"
insecure = true

[[registry]]
location = "127.0.0.1:5000"
insecure = true
```

Then restart the podman daemon for this to take effect.

## Step 7: Build and Push Critic Image

```bash
# Push directly to the local registry (avoids loading into podman local storage)
export TMPDIR=/dev/shm
bazel run //props/agents/critic:push -- \
  --repository localhost:5000/critic --tag latest
```

Verify: `curl http://localhost:5000/v2/critic/tags/list`

### Pre-pull the Image in Podman

```bash
TMPDIR=/dev/shm podman pull --tls-verify=false 127.0.0.1:5000/critic:latest
```

The backend also needs to pull the image. Having it pre-pulled avoids issues.

## Step 8: Create Props Config File

Create `/tmp/props-ollama-config.toml`:

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

## Step 9: Start the Props Backend

```bash
export PROPS_CONFIG_FILE=/tmp/props-ollama-config.toml
export OLLAMA_DUMMY_KEY=dummy
export PGHOST=127.0.0.1 PGPORT=5432 PGUSER=postgres
export PGPASSWORD="props-bench-dcfc0ef9506c6673"
export PGDATABASE=eval_results
export ADGN_PROPS_SPECIMENS_ROOT="$PWD/props/specimens"
export DOCKER_HOST="unix:///tmp/claude-podman-*.sock"
export PROPS_DOCKER_NETWORK=host
export TMPDIR=/dev/shm

bazel run //props/backend:backend_cli -- serve --host 127.0.0.1 --port 8000
```

**Critical env vars**:

- `DOCKER_HOST` — points to the podman socket
- `PROPS_DOCKER_NETWORK=host` — agent containers use host networking (required
  for podman/gVisor where bridge networking isn't available)
- `TMPDIR=/dev/shm` — prevents image layer downloads from filling root `/tmp`

The backend logs an admin token on startup.

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

TODO: Document grader run once critic completes.

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
