# Benchmark Results — OLLAMA_NUM_CTX=1048576

Date: 2026-02-23
Hardware: 2x NVIDIA RTX 5090 (64 GB VRAM total)
Config: `OLLAMA_KV_CACHE_TYPE=q8_0`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_NUM_CTX=1048576`
Endpoint: `https://litellm.allegedly.works/v1` (LiteLLM proxy)
Seed: 42, time limit: 60s per config, NIAH samples: 8

## gpt-oss-20b-128k

| Metric  | Mean (t/s) |    Stdev |   n |     NIAH |
| ------- | ---------: | -------: | --: | -------: |
| tg128   |        N/A |          |     |          |
| pp1k    |    4,002.8 |    857.1 |  10 | 6/6 100% |
| pp4k    |    5,731.5 |  4,204.5 |   8 | 2/2 100% |
| pp16k   |   12,719.7 | 14,313.3 |   5 | 3/3 100% |
| pp32k   |   14,865.2 | 12,820.8 |   5 |   0/1 0% |
| pp64k   |    1,054.8 |      0.7 |   2 | 1/1 100% |
| pp128k  |    1,403.3 |        - |   1 |   0/1 0% |
| pp256k  |    2,324.5 |    176.7 |   2 |   0/1 0% |
| pp512k  |    1,342.6 |        - |   1 |   0/1 0% |
| pp1000k |    1,155.9 |        - |   1 |   0/1 0% |

Notes:

- Throughput cliff at pp64k (14.9k → 1.1k t/s)
- NIAH recall degrades sharply at 32k+ (only 1 sample per size due to 60s limit)
- tg128 N/A: reasoning model streaming doesn't emit delta.content tokens
- High stddev at pp4k-pp32k suggests KV cache reallocation between requests

## gpt-oss-120b-128k

| Metric  | Mean (t/s) | Stdev |   n |     NIAH |
| ------- | ---------: | ----: | --: | -------: |
| tg128   |        N/A |       |     |          |
| pp1k    |       52.2 |  45.1 |   2 | 1/1 100% |
| pp4k    |       66.7 |     - |   1 | 1/1 100% |
| pp16k   |      261.5 |     - |   1 | 1/1 100% |
| pp32k   |      316.9 |     - |   1 | 1/1 100% |
| pp64k   |    1,274.3 | 579.0 |   2 | 1/1 100% |
| pp128k  |    1,521.6 |     - |   1 | 1/1 100% |
| pp256k  |    1,539.1 |     - |   1 | 1/1 100% |
| pp512k  |    1,507.4 |     - |   1 |   0/1 0% |
| pp1000k |    1,517.7 |     - |   1 |   0/1 0% |

Notes:

- No throughput cliff; smooth scaling from 52 t/s (1k) to ~1.5k t/s (64k+)
- Much better NIAH: 7/9 (passes through 256k vs 20b failing at 32k)
- PP throughput plateaus at ~1.5k t/s from 64k+ (similar to 20b at large contexts)
- Prewarm: 32.7s (vs 9.0s for 20b)
- NIAH samples take 60-270s each (model reasoning time)

## Observations

1. **OLLAMA_NUM_CTX=1048576 likely causes excessive KV cache preallocation**, degrading
   both throughput and NIAH recall compared to the earlier OLLAMA_NUM_CTX=131072 benchmark
   (which showed pp scaling from 3.5k to 133k t/s and perfect NIAH at 128k).
2. **120b outperforms 20b on NIAH** despite being 6x larger — better long-context capability.
3. **Both models converge to ~1.5k t/s at 128k+** — likely memory-bandwidth bottleneck.
4. **Sample counts are low** at large contexts (1 per size) due to 60s time limit.
