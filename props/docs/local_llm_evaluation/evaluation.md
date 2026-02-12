# Running Props Evaluation with a Local LLM

End-to-end procedure for running the props critic and grader against a committed
specimen snapshot using a local open-weight LLM served via llama-server.

See <benchmarks.md> for model performance data on this environment.

## Prerequisites

- Claude Code web session with `claude_hooks` (provides podman, `/dev/shm`,
  insecure-registry entries, `DOCKER_HOST` env var)
- Enough free RAM for the model + KV cache (see memory estimates below) plus
  ~2 GiB for PostgreSQL, registry, and backend containers

## Tested Models

| Model       | GGUF (Q4_K_M)              | Download | RAM (loaded) |   tg128 | Tool calling |
| ----------- | -------------------------- | -------: | -----------: | ------: | ------------ |
| gpt-oss-20b | `unsloth/gpt-oss-20b-GGUF` | 10.8 GiB |      ~12 GiB | ~12 t/s | untested     |
| Qwen3-8B    | `unsloth/Qwen3-8B-GGUF`    |  4.7 GiB |       ~6 GiB |  ~9 t/s | works        |

**Recommendation**: Qwen3-8B fits comfortably in the 21 GiB environment and
leaves headroom for all services. gpt-oss-20b is faster at generation but
tight on RAM.

## Memory Estimates (Qwen3-8B)

Model weights: ~4.7 GB. Process overhead (compute buffers, allocator): ~3.5 GB.
The remaining budget goes to the KV cache, whose size depends on context length
and cache quantization (`-ctk`/`-ctv` flags).

Qwen3-8B KV cache: 36 layers, 8 GQA heads, 128 head dim.

| `--ctx-size` | KV (f16) | KV (q8_0) | KV (q4_0) | Total (q4_0) |
| -----------: | -------: | --------: | --------: | -----------: |
|         4096 |  0.56 GB |   0.28 GB |   0.14 GB |       8.3 GB |
|         8192 |  1.12 GB |   0.56 GB |   0.28 GB |       8.5 GB |
|        16384 |  2.25 GB |   1.12 GB |   0.56 GB |       8.8 GB |
|        32768 |  4.50 GB |   2.25 GB |   1.12 GB |       9.3 GB |

With q4_0 KV cache (`-ctk q4_0 -ctv q4_0`), the full 32K native context fits
comfortably under the 15 GB `process_api` kill threshold (~9.3 GB measured).
The critic agent's system prompt and tool definitions consume ~2K tokens, so
4096 total context is too small for useful code analysis. **Use 32768** (the
model's full native context window).

> **TODO**: Explore extending beyond 32K with RoPE scaling (`--rope-freq-scale`,
> `--rope-freq-base`) or YaRN. Qwen3-8B may support longer contexts with
> appropriate scaling, and the RAM budget allows it at q4_0.

## Step 1: Download llama-server

```bash
mkdir -p /tmp/benchmark
LLAMA_VERSION=b7993
ASSET_URL=$(curl -s "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/${LLAMA_VERSION}" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(next(a['url'] for a in d['assets'] if a['name'] == f'llama-{\"${LLAMA_VERSION}\"}-bin-ubuntu-x64.tar.gz'))")
curl -L -H "Accept: application/octet-stream" \
  -o /tmp/benchmark/llama-server.tar.gz "$ASSET_URL"
tar -xzf /tmp/benchmark/llama-server.tar.gz -C /tmp/benchmark
```

~24 MB download. The CPU-only build is sufficient for this environment.

> **Note**: `curl -sL | tar` fails because GitHub redirects to a different
> domain. Download to a file first, then extract.

## Step 2: Download the GGUF Model

Download to `/tmp` (disk-backed), **not** `/dev/shm` (tmpfs). Files on `/dev/shm`
are evicted when the process that mmapped them exits.

```bash
# Option A: Qwen3-8B (recommended — 4.7 GiB, leaves RAM headroom)
curl -L --progress-bar -o /tmp/benchmark/Qwen3-8B-Q4_K_M.gguf \
  "https://huggingface.co/unsloth/Qwen3-8B-GGUF/resolve/main/Qwen3-8B-Q4_K_M.gguf"

# Option B: gpt-oss-20b (10.8 GiB, faster generation, tight on RAM)
curl -L --progress-bar -o /tmp/benchmark/gpt-oss-20b-Q4_K_M.gguf \
  "https://huggingface.co/unsloth/gpt-oss-20b-GGUF/resolve/main/gpt-oss-20b-Q4_K_M.gguf"
```

## Step 3: Start Inference Server

```bash
MODEL_FILE=/tmp/benchmark/Qwen3-8B-Q4_K_M.gguf  # or gpt-oss-20b-Q4_K_M.gguf

LD_LIBRARY_PATH="/tmp/benchmark/llama-b7993" \
  nohup /tmp/benchmark/llama-b7993/llama-server \
    --model "$MODEL_FILE" \
    --host 127.0.0.1 --port 11434 \
    --ctx-size 32768 --parallel 1 \
    -ctk q4_0 -ctv q4_0 \
    --jinja \
    --no-warmup --cache-ram 0 \
    > /dev/shm/llama.log 2>&1 &
```

Wait for the health endpoint: `curl -s http://127.0.0.1:11434/health`

**Key flags**:

- `--ctx-size 32768` — Qwen3-8B's full native context window. The critic
  agent's system prompt + tool definitions consume ~2K tokens; 4096 is too
  small for useful code analysis. 32K fits in ~9.3 GB with q4_0 KV cache.
- `-ctk q4_0 -ctv q4_0` — quantize the KV cache to 4-bit. Reduces KV cache
  memory by 4x vs the default f16, enabling larger context windows within the
  RAM budget.
- `--jinja` — enables chat template processing; required for tool calling
  (Qwen3-8B). Harmless for models that don't use it.
- `--no-warmup --cache-ram 0` — skip KV cache pre-allocation to save RAM.
- `--parallel 1` — single slot is sufficient since we run one request at a time.

Verify: `curl -s http://127.0.0.1:11434/v1/models`

The model name reported by llama-server is the GGUF filename (e.g.,
`Qwen3-8B-Q4_K_M.gguf`). This must match `upstream_model` in the config
(Step 6).

`llama-server` exposes `/v1/chat/completions`, `/v1/responses`, and
`/v1/models`. The props backend proxies `/v1/responses` (the OpenAI Responses
API) to the upstream.

## Step 4: Start PostgreSQL

```bash
mkdir -p /dev/shm/pgdata

TMPDIR=/dev/shm podman run -d --name postgres --network=host \
  -e POSTGRES_PASSWORD=props-bench-dcfc0ef9506c6673 \
  -e POSTGRES_DB=eval_results \
  -v /dev/shm/pgdata:/var/lib/postgresql/data \
  docker.io/library/postgres:16
```

Data is on tmpfs — it won't survive reboots. For persistence, use a disk-backed
path instead.

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
and creates admin credentials. No manual model metadata insert is needed — the
backend syncs custom models from the config file's `[[models]]` entries on
startup.

## Step 6: Start OCI Registry

```bash
TMPDIR=/dev/shm podman run -d --name registry --network=host \
  docker.io/library/registry:2
```

Verify: `curl http://localhost:5000/v2/_catalog`

The props backend includes a built-in OCI registry proxy (`/v2/*` routes) that
records `agent_definitions` on push. The proxy forwards to this upstream
`registry:2` on port 5000. Images are pushed to the **backend** (port 8000),
not directly to the upstream registry.

## Step 7: Create Props Config File

Copy <props_config.toml> to `/tmp/props-ollama-config.toml` and edit the
`upstream_model` to match your chosen GGUF filename:

```bash
cp props/docs/local_llm_evaluation/props_config.toml /tmp/props-ollama-config.toml
# Edit upstream_model if using a different model than the default
```

The config defines:

1. **`grader_model`**: Tells the backend to start the `GraderSupervisor`, which
   automatically runs a grader after each critic finishes. Without this, grading
   must be triggered manually.
2. **Upstream connection** (`[upstreams.ollama]`): llama-server on port 11434.
   `api_key_env` points to a dummy env var (llama-server ignores API keys).
3. **Custom model** (`[[models]]`): Declares the local model with zero pricing
   (inference is free). The `upstream_model` field must match the GGUF filename
   reported by `/v1/models`.

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
- `PROPS_REGISTRY_UPSTREAM_URL` — forwards to upstream `registry:2` on port 5000
- `TMPDIR=/dev/shm` — prevents image layer downloads from filling root `/tmp`

The backend logs an admin token on startup — a base64-encoded
`username:password` string (e.g., `cG9zdGdyZXM6cHJvcHMt...`). This token is
used for all authenticated API calls.

**Auth format**: The backend accepts both `Bearer` and `Basic` schemes with
the same payload — base64 of `username:password`:

```bash
# Both of these work:
-H "Authorization: Bearer $ADMIN_TOKEN"
-H "Authorization: Basic $ADMIN_TOKEN"
```

The `Bearer` scheme is used for API calls (curl, OpenAI SDK). The `Basic`
scheme is used by OCI/Docker tooling (crane, docker push/pull).

## Step 9: Build and Push Agent Images

Push through the backend's registry proxy (port 8000), not directly to the
upstream registry (port 5000). The proxy records `agent_definitions` in the DB.

The push uses HTTP Basic auth. Set up a Docker config for crane:

```bash
export TMPDIR=/dev/shm
ADMIN_TOKEN="<from backend startup logs>"

# Create Docker config for crane auth
mkdir -p /tmp/crane-config
echo "{\"auths\":{\"localhost:8000\":{\"auth\":\"$ADMIN_TOKEN\"}}}" \
  > /tmp/crane-config/config.json

# Push critic image
DOCKER_CONFIG=/tmp/crane-config \
  bazel run //props/agents/critic:push -- \
    --repository localhost:8000/critic --tag latest --insecure

# Push grader image
DOCKER_CONFIG=/tmp/crane-config \
  bazel run //props/agents/grader:push -- \
    --repository localhost:8000/grader --tag latest --insecure
```

## Step 10: Run a Critic

```bash
ADMIN_TOKEN="<from backend logs>"
MODEL_NAME="local-llm"  # matches [[models]] name in config

curl -s -X POST "http://localhost:8000/api/runs/critic" \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "definition_id": "latest",
    "example": {"kind": "whole_snapshot", "snapshot_slug": "wt/2025-01-03-00"},
    "critic_model": "'"$MODEL_NAME"'",
    "timeout_seconds": 1800,
    "budget_usd": 0.0
  }'
```

`budget_usd=0.0` — local models have zero cost, so no budget is needed.

## Step 11: Wait for Grading

The `GraderSupervisor` (enabled by `grader_model` in the config) automatically
runs a grader after each critic finishes. No manual curl is needed.

Monitor progress by querying the database:

```bash
PGPASSWORD="props-bench-dcfc0ef9506c6673" psql -h 127.0.0.1 -U postgres -d eval_results \
  -c "SELECT agent_run_id, type_config->>'agent_type' AS type, model, status,
             container_exit_code FROM agent_runs ORDER BY created_at"
```

Wait until both the critic and grader runs show `status = 'exited'` with
`container_exit_code = 0`.

## Step 12: Export Results

After each agent run completes, export the run results (excluding ground truth
tables) to a SQL dump in the repo:

```bash
PGPASSWORD="props-bench-dcfc0ef9506c6673" pg_dump -h 127.0.0.1 -U postgres eval_results \
  --data-only --no-owner --no-privileges \
  --exclude-table=true_positives \
  --exclude-table=true_positive_occurrences \
  --exclude-table=false_positives \
  --exclude-table=false_positive_occurrences \
  --exclude-table=fp_occurrence_relevant_files \
  --exclude-table=occurrence_ranges \
  --exclude-table=critic_scopes_expected_to_recall \
  --exclude-table=file_sets \
  --exclude-table=file_set_members \
  --exclude-table=snapshots \
  --exclude-table=snapshot_files \
  --exclude-table=model_metadata \
  --exclude-table=agent_role_salt \
  --exclude-table=alembic_version \
  > props/docs/local_llm_evaluation/results.sql
```

This exports agent runs, reported issues, grading edges, LLM requests, and
cluster data — everything needed to analyze evaluation results — while excluding
ground truth labels, snapshot content, and infrastructure tables.

## Troubleshooting

### GGUF file vanishes after llama-server exits

Files on `/dev/shm` (tmpfs) are backed by RAM. When the process that mmapped
the file exits, the kernel may reclaim the pages. Always store GGUF files on
`/tmp` (disk-backed), not `/dev/shm`.

### `no space left on device` during image pull

Podman stores image layers in `TMPDIR` (defaults to `/tmp`). Set
`TMPDIR=/dev/shm` before any podman operation to use tmpfs.

### `http: server gave HTTP response to HTTPS client`

The `claude_hooks` session start hook configures insecure registries. Verify
`/root/.cache/claude-hooks/podman/registries.conf` contains entries for
`localhost:5000` and `127.0.0.1:5000` with `insecure = true`.

### Agent container can't reach services

Set `PROPS_DOCKER_NETWORK=host` so agent containers share the host network
namespace and can reach `127.0.0.1:*` services.

### llama-server download: `curl -sL | tar` fails

GitHub release asset downloads redirect to a different domain. Piping `curl -sL`
directly to `tar` can fail if the redirect returns an HTML error page. Download
to a file first, then extract.
