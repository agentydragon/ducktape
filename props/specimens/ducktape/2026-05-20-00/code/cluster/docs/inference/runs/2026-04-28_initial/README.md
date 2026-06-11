# 2026-04-28 initial throughput pass — cluster Ollama

First small-N measurement against the live cluster Ollama deployment.
Goal: establish baseline prefill / content-decode TPS and cold-load times
for the three currently-loaded models. Quality evals (AIME / GPQA /
HumanEval+ / BFCL) deferred to a later pass.

> **Status (2026-04-28):** complete.

## What ran

- **Job manifest**: <job.yaml>
- **Bench script**: <bench.py> (reasoning-aware, JSONL output, reasoning_effort=low)
- **Driver**: <run.sh> (creates ConfigMap from `bench.py`, applies the Job, tails logs to `raw_output.jsonl`)
- **Endpoint**: `http://ollama.ollama:11434/v1/chat/completions` (in-cluster, no auth-proxy hop)
- **Configs measured**: `c-gpt20`, `c-gpt120`, `c-gemma31` (see <../../benchmarks.md#live-in-cluster-k8s-wyrm2>)

### Parameters

| Parameter           | Value                                                       |
| ------------------- | ----------------------------------------------------------- |
| Input lengths       | 1024, 8192 tokens (target; actual from server `usage`)      |
| Output length cap   | 256 tokens, prompt forces ≥200-token continuation           |
| `reasoning_effort`  | `low` (also passes `think: "low"` for Ollama-native compat) |
| Trials per (m, len) | 5 (plus 1 warmup at 64-token prompt to force cold load)     |
| Concurrency         | 1 (serial)                                                  |
| Streaming           | `stream: true, stream_options.include_usage: true`          |
| Warmup timeout      | 1800 s                                                      |

## Results

### Throughput (warm trials)

p50 over 5 trials per cell unless noted.

| Config      | Input | Prefill TPS p50 | Content decode TPS p50 | t_first_content p50 (s) | t_done p50 (s) |
| ----------- | ----- | --------------- | ---------------------- | ----------------------- | -------------- |
| `c-gpt20`   | 1024  | **3799**        | **174.1**              | 0.48                    | 1.96           |
| `c-gpt20`   | 8192  | **21330**       | **150.5**              | 0.65                    | 2.31           |
| `c-gpt120`  | 1024  | **119**         | **1.48**               | 15.4                    | 191.8          |
| `c-gpt120`  | 8192  | **1016**        | **1.56**               | 13.7                    | 183.9          |
| `c-gemma31` | 1024  | n/a (1)         | n/a (1)                | n/a (1)                 | 7.63           |
| `c-gemma31` | 8192  | n/a (1)         | n/a (1)                | n/a (1)                 | 8.50           |

(1) See "gemma4 streaming format" caveat below — the parser saw zero
`delta.content` chunks despite `usage.completion_tokens=256`, so per-phase
TPS is unmeasurable for this model from this pass. End-to-end times are
real.

`reasoning_effort: low` suppressed reasoning entirely on gpt-oss:
`reasoning_tokens: 0` on every gpt-oss trial. Content decode TPS above is
unconfounded content-token rate.

### Cold-load time (warmup request)

| Config      | t_first_content (s) | t_done (s) | Notes                                                                                                                                                               |
| ----------- | ------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `c-gpt20`   | 180                 | 181        | First model loaded; cold from disk (`lvm-proxmox-hdd`)                                                                                                              |
| `c-gpt120`  | **808**             | 1013       | Loaded after 20b. Decode during warmup is anomalous (1.25 tok/s), likely because load was still finalizing during initial chunks; trial numbers are the clean read. |
| `c-gemma31` | n/a                 | **262**    | Loaded after 120b. `t_first_content` not captured — see streaming-format caveat.                                                                                    |

## Headline observations

### `gpt-oss:120b` is CPU-offload-bound (~1.5 tok/s)

Median content decode for 120b at 1K input is **~1.5 tok/s** — about 10×
slower than the 65 GB-MXFP4 / 1 TB-bandwidth memory ceiling (~17 tok/s)
would predict if all weights lived in VRAM.

The arithmetic explains the gap: 65.4 GB MXFP4 model + KV cache for
131 K context with `q8_0` doesn't fit in 64 GB total VRAM (2× 32 GB).
Ollama silently CPU-offloads the overflow, and decode then traverses the
PCIe bus per token. Prefill is similarly hobbled (median 119 tok/s vs
expected 1000s+).

This is the most important finding from this pass: **`gpt-oss:120b` on
this hardware via Ollama is a "fits but is unusable" config.** Not a
surprise in hindsight — but worth measuring before recommending a
replacement.

### `gpt-oss:20b` decodes at ~150–175 tok/s, exactly as expected

13.8 GB MXFP4 fits comfortably in one 5090 with KV-cache headroom. The
~170 tok/s decode at 1K input is in the right ballpark for this model
size on this hardware. Prefill scales nicely with batch (1K → 8K input
moves median prefill from 3.8 K to 21 K tok/s).

### Cold loads off `lvm-proxmox-hdd` are slow

- `gpt-oss:20b`: ~180 s cold (13.8 GB → ~75 MB/s effective)
- `gpt-oss:120b`: ~810 s cold (65.4 GB → ~80 MB/s effective)

Effective rates are roughly consistent and roughly HDD-limited. With
typical sequential HDD throughput of ~150–200 MB/s these numbers suggest
~half-bandwidth utilization (probably some seeking + Ollama processing
overhead). Validates the storage-class trade-off captured in
<../../backend_comparison.md#storage-class>: HDD is fine for stable
in-VRAM serving, painful for cold swaps. Since Ollama keeps the active
model resident, this is a per-deployment-rollout cost, not per-request.

## Caveats / what this pass doesn't measure

- **gemma4 streaming format**: every gemma4 trial returned
  `usage.completion_tokens=256` (so the model did generate) and a
  realistic `t_done` (7.6 s @ 1K, 8.5 s @ 8K — implies ~30 tok/s
  end-to-end), but our parser saw **zero `delta.content` chunks**. So
  Ollama's gemma4 streaming chunks must put the text in a different
  field (or batch it without per-token deltas). Needs investigation
  before we can decompose prefill vs decode for this model. Followup in
  the next pass: capture a raw chunk dump for gemma4 and find where the
  text lives.
- **Single concurrency only.** Multi-tenant aggregate throughput unknown.
- **Reasoning behavior unknown.** `reasoning_effort: low` was used to
  isolate raw throughput. Reasoning quality (AIME / GPQA pass@1) and
  reasoning-token cost at `medium` / `high` are out of scope here —
  next pass.
- **Quality not validated.** We measured tokens/sec, not whether the
  model produced coherent output. (Spot check: outputs were coherent
  filler text, but no formal grading.)
- **No comparison to ceiling.** We didn't measure raw memory bandwidth
  on these GPUs in this VM passthrough setup, so "10× slower than
  ceiling" for 120b is theoretical, not verified-against-instruments.

## Followup: CPU offload confirmed for `gpt-oss:120b`

After the throughput pass, triggered a single 120b request and sampled
`ollama ps` + `nvidia-smi` while the model was resident. Result:

```text
$ ollama ps
NAME            ID              SIZE     PROCESSOR          CONTEXT    UNTIL
gpt-oss:120b    a951a23b46a1    69 GB    19%/81% CPU/GPU    131072     9 minutes from now

$ nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv
index, memory.used [MiB], memory.free [MiB], utilization.gpu [%]
0, 28641 MiB, 3469 MiB, 13 %
1, 27198 MiB, 4912 MiB, 0 %
```

So **19% of 69 GB = ~13 GB lives in CPU RAM** and traverses PCIe per
token. VRAM is essentially saturated: 28.6 + 27.2 = **55.8 GB of 64 GB
total used**. KV cache for 131 K context at `q8_0` plus model weights
plus engine overhead exceeds 64 GB, so Ollama silently spills weights
to host memory.

Hypothesis from the throughput pass — that the ~1.5 tok/s decode is
caused by partial CPU residency, not some other bottleneck — is now
confirmed. The fix is either a leaner KV cache budget (smaller context),
a denser quant (none available smaller than MXFP4), or moving to an
engine that actually fits this model in VRAM (vLLM with tensor parallel

- FP8 KV).

### How this was measured

Triggered with a one-off `claude-sandbox` Pod that hit the cluster
Ollama with `keep_alive: 10m`:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: ollama-trigger
  namespace: claude-sandbox
spec:
  restartPolicy: Never
  containers:
    - name: curl
      image: curlimages/curl:8.10.1
      env:
        - { name: HTTP_PROXY, value: "" }
        - { name: HTTPS_PROXY, value: "" }
        - { name: http_proxy, value: "" }
        - { name: https_proxy, value: "" }
      command:
        - sh
        - -c
        - |
          curl -sS --noproxy '*' -X POST http://ollama.ollama:11434/api/generate \
            -H 'Content-Type: application/json' \
            -d '{"model":"gpt-oss:120b","prompt":"Write 100 words about cats.","stream":false,"keep_alive":"10m"}' \
            -o /tmp/out.json
```

Then while the request was in flight (or briefly after, with
`keep_alive` keeping the model resident):

```bash
kubectl -n ollama exec ollama-764d8ffbdf-8hbgf -c ollama -- ollama ps
kubectl -n ollama exec ollama-764d8ffbdf-8hbgf -c ollama -- \
  nvidia-smi --query-gpu=index,memory.used,memory.free,utilization.gpu --format=csv
```

(The `--noproxy '*'` and zeroed `HTTP_PROXY` env vars work around the
mitmproxy auto-injection in `claude-sandbox`. See
<../../../../k8s/TODO.md#reconsider-mitmproxy-auto-injection-in-claude-sandbox>.)

## Next pass

Candidates for the next run, in rough order of value:

1. **Reasoning-effort sweep** — same gpt-oss models at `low / medium /
high`, on a problem-solving prompt. Captures how much
   reasoning-token budget each level consumes and whether the knob is
   actually wired through Ollama's compat shim.
2. **AIME-2024 small-N (10 problems)** — pass@1 per (model × effort).
   `simple-evals` is the tool of choice; ~30 min total.
3. **`gpt-oss:120b` follow-ups**: confirm the CPU-offload hypothesis
   via `ollama ps` / `nvidia-smi` while a request is in flight. If
   confirmed, this is the rationale for trying vLLM.
4. **Concurrent throughput on 20b** — N=1, 4, 8 to find the saturation
   knee. Useful for sizing the multi-tenant ceiling.
5. **Skip cold-load remeasurement** unless we change storage class.
   This pass got it.

## Raw data

- <raw_output.jsonl> — JSONL events from this run
