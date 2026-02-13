# Running Props Evaluation with a Local LLM

End-to-end procedure for running the props critic and grader against committed
specimen snapshots using a local open-weight LLM served via llama-server.

See <benchmarks.md> for model performance data.

## Prerequisites

- A running props stack: PostgreSQL, OCI registry, backend with agent images
  pushed. The `/test_props setup` skill automates this.
- Enough free RAM for the model + KV cache (see memory estimates below) plus
  ~2 GiB for the props stack containers

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

## Step 1: Download and Start llama-server

```bash
mkdir -p /tmp/benchmark
LLAMA_VERSION=b7993
ASSET_URL=$(curl -s "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/${LLAMA_VERSION}" | \
  python3 -c "import json,sys; d=json.load(sys.stdin); print(next(a['url'] for a in d['assets'] if a['name'] == f'llama-{\"${LLAMA_VERSION}\"}-bin-ubuntu-x64.tar.gz'))")
curl -L -H "Accept: application/octet-stream" \
  -o /tmp/benchmark/llama-server.tar.gz "$ASSET_URL"
tar -xzf /tmp/benchmark/llama-server.tar.gz -C /tmp/benchmark
```

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

Wait for health: `curl -s http://127.0.0.1:11434/health`

**Key flags**:

- `--ctx-size 32768` — full native context window (see memory estimates above)
- `-ctk q4_0 -ctv q4_0` — 4-bit KV cache quantization (4x RAM savings)
- `--jinja` — chat template processing; required for tool calling (Qwen3-8B)
- `--no-warmup --cache-ram 0` — skip KV cache pre-allocation to save RAM
- `--parallel 1` — single slot (one request at a time)

The model name reported by `/v1/models` is the GGUF filename (e.g.,
`Qwen3-8B-Q4_K_M.gguf`). This must match `upstream_model` in the config.

## Step 4: Configure the Props Backend for Local LLM

Copy <props_config.toml> and edit `upstream_model` to match your GGUF filename:

```bash
cp props/docs/local_llm_evaluation/props_config.toml /tmp/props-ollama-config.toml
```

The config defines:

1. **`grader_model`**: Enables the `GraderSupervisor` (auto-grades after each
   critic finishes)
2. **Upstream** (`[upstreams.ollama]`): llama-server on port 11434. `api_key_env`
   points to a dummy env var (llama-server ignores API keys)
3. **Custom model** (`[[models]]`): Zero pricing (local inference is free).
   `upstream_model` must match the GGUF filename from `/v1/models`

Pass `PROPS_CONFIG_FILE=/tmp/props-ollama-config.toml` and
`OLLAMA_DUMMY_KEY=dummy` when starting the backend.

## Step 5: Run a Critic

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

`budget_usd=0.0` — local models have zero cost.

Monitor and wait for grading as described in
<../openai_evaluation/evaluation.md> ("Running Critics" section).

## Exporting Results

Same procedure as OpenAI evaluation — see
<../openai_evaluation/evaluation.md> ("Exporting Results" section), adjusting
the output path:

```bash
pg_dump eval_results \
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
  -f props/docs/local_llm_evaluation/results.sql \
  && zstd --rm --ultra -22 props/docs/local_llm_evaluation/results.sql
```

## Troubleshooting

### GGUF file vanishes after llama-server exits

Files on `/dev/shm` (tmpfs) are backed by RAM. When the process that mmapped
the file exits, the kernel may reclaim the pages. Always store GGUF files on
`/tmp` (disk-backed), not `/dev/shm`.

### llama-server download: `curl -sL | tar` fails

GitHub release asset downloads redirect to a different domain. Download to a
file first, then extract.
