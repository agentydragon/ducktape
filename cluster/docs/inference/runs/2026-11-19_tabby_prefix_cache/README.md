# TabbyAPI prefix-cache probe on wyrm2

On November 19 Pacific, stock Qwen3.8 Flash-Next EXL3 used the existing two
5090s, 270,336-token Q8 GPU pool, one active request, and `[22, 30]` placement
budgets. The control used GPU caching. The trial enabled an 8 GiB RAM tier
and a 16 GiB NVMe tier together on ExLlamaV3 1.6.7. See the
[GitOps change](https://github.com/agentydragon/ducktape/pull/18022) and
[experiment runbook](../../../../cdk8s/ollama/prefix-cache-probe.md).

The client streamed directly to TabbyAPI with usage enabled. It used a fixed
synthetic inventory ledger with 211,024 rendered tokens and repeated the
exact request within each arm. Different tags at the beginning kept the arms
and eviction prefixes distinct. Two other stock requests of 211,026 tokens
evicted the first prefix. A separate short abliterated request forced a
checkpoint switch before replaying the original stock prefix.

| Replay condition               | GPU-only first text | Trial first text | Trial cached tokens |
| ------------------------------ | ------------------: | ---------------: | ------------------: |
| GPU warm                       |              0.62 s |           0.65 s |             210,944 |
| Same checkpoint after eviction |             37.52 s |           1.52 s |             210,944 |
| Stock → abliterated → stock    |             78.96 s |          37.41 s |             210,944 |

The same-checkpoint replay promoted 824 RAM pages and zero disk pages. On the
return to stock, the engine logged restoring 823 NVMe pages (210,688 tokens)
and one recurrent checkpoint. The model load took 30.1 s and native prefill
took 80 ms for the remaining 80 tokens. First-text latency includes model
switching and page restore. The GPU-only return ran a fresh 31.45 s prefill.

The first abliterated load in the new pod took 197.2 s, including 162.5 s of
warmup. That cold load is a separate cost from reusing a stock prefix. The
lowest sampled free VRAM during the cache arm was 5,668 MiB on GPU 0 and
4,771 MiB on GPU 1. The results are one live pass. RAM hits were identified by
allocation counters with both tiers enabled. The probe exercised checkpoint
unload and reload within the running pod.

## Replay after a pod replacement

The [permanent GitOps rollout](https://github.com/agentydragon/ducktape/pull/18045)
started a new pod with both caches enabled. After readiness, the exact same
synthetic request reused 210,944 of 211,024 prompt tokens. TabbyAPI logged
restoring 823 NVMe pages (210,688 tokens) and one recurrent checkpoint.
First text arrived in 5.55 s, including page restore and 0.86 s of native
prefill. Model load and warmup took 221.2 s before the replay began.
