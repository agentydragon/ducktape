# Dual-5090 Flash-Next SSD comparison

November 18, 2026, wyrm2. Both model directories were on the same ext4
filesystem on Atlas NVMe at `/games/llm-models/`. Ollama was scaled to zero
through [its GitOps pause](https://github.com/agentydragon/ducktape/pull/17635)
during the host tests. The desktop session remained active. The benchmark
servers listened only on loopback and ran sequentially.

## Configuration

- **EXL3:** turboderp's self-calibrated 2.68-bit checkpoint, revision
  [d660113](https://huggingface.co/turboderp/Qwen3.8-Flash-Next-exl3/tree/d660113325e1eba20e760a9063580139bdbdfc22).
  All published weight SHA-256 digests matched. Official TabbyAPI image
  `sha256:6daf422856272925b8fa0db0e697b9cc7b68e5c557b2789fcf84d6e54139dfd6`,
  ExLlamaV3 1.6.7 and PyTorch 2.9.0/CUDA 12.8. Layer split `[28, 30]`, no CPU
  expert offload, NVMe-backed n-gram table, Q8 cache with 270,336 tokens,
  262,144 maximum sequence, one active slot, native MTP depth 3. Uses the
  official image with default kernels.
- **GGUF control:** the August IQ4_XS shards copied read-only from Ollama and
  checked against the three [deployment hashes](../../../../cdk8s/ollama/qwen38-ssd-shards.tsv).
  CUDA llama.cpp build 12464, commit `d3c3e242c`, image
  `sha256:d8d71f3b200080bfa67d65339cca8af7f3a506ede922323cc0dd35389a2a3534`.
  One slot, 262,144 context, lazy n-gram loading, layer split, Q8 KV and
  4/2 GiB fit margins. Same-weight MTP uses the
  [official Q4_0 sidecar](https://huggingface.co/ggml-org/Qwen3.8-Flash-Next-GGUF/tree/a3e717e66206086bffa2be26abbbf22e84e93f36)
  at depth 3.

## Measurement

Streamed text completions at temperature zero. A short fixed prompt tests
decode before and after startup warmup. Each long case prefixes a marker
question with the same source text across engines, then asks for a 512-token
review using the same prefix. The corpus is the ordered Python sources
(non-test files up to 32,000 bytes) from `git ls-files util cluster/cdk8s` at
`e7017d6e85`, truncated with the checkpoint tokenizer to 32,000, 128,000 and
240,000 tokens before adding the marker and questions. The probe uses 32
generated tokens for marker recall. TabbyAPI uses `min_tokens` equal to the
output cap. After llama.cpp returned only an EOS on the initial 32K prompt,
the timed rerun set `ignore_eos` for its text probes. Chat probes used normal
stopping for both engines.

Startup is time from container launch to its ready models endpoint. The
runner requested eviction of its staged weight files from the guest page
cache before loading each arm. This leaves Atlas's ZFS cache alone. Prefill
and decode are measured separately. Time to first token is measured at the
stream client. Warm decode rate uses visible first-to-last output time and
the reported completion count.

The API smoke checks cover automatic and forced tool selection, an actual
tool result, strict JSON, and a mid-conversation system message, both direct
and through a localhost LiteLLM 1.100.0 proxy. Coding smoke asks the model to
write a closed-interval merge function and runs six cases in a networkless
container. Marker recall and this one coding task are smoke checks.

## Matched SSD results

One cold and one warm sample per context. The warm results generated 512
tokens. The same prompt token counts applied across both backends.

| Input                | IQ4_XS cold TTFT (s) | EXL3 cold TTFT (s) | IQ4_XS warm decode (tok/s) | EXL3 warm decode (tok/s) |
| -------------------- | -------------------: | -----------------: | -------------------------: | -----------------------: |
| 41-token short probe |                 1.24 |              33.85 |                      42.28 |                   127.96 |
| 32K                  |                66.07 |               7.62 |                      45.72 |                   129.59 |
| 128K                 |               250.16 |              22.59 |                      38.47 |                   165.54 |
| 240K                 |               441.72 |              40.28 |                      33.10 |                   167.23 |

The short cold row is the first request after startup. Model readiness took
73.5 seconds on llama.cpp and 63.8 seconds on TabbyAPI. At 240K, the warm
continuation took 17.0 vs 3.42 seconds end to end. New input in that continuation
was 518 tokens in llama.cpp and 172 in TabbyAPI. The latter accepted 375 of 411
draft tokens on this request, so decode speed can vary with the output.

Peak sampled GPU memory, including the desktop, was 28,181/29,862 MiB on
llama.cpp and 31,567/21,362 MiB on TabbyAPI. The TabbyAPI split left about
1,040 MiB on GPU0 in this run.

Stock TabbyAPI recalled all three markers within the 32-token cap. IQ4_XS on
llama.cpp started reasoning under that cap, with no marker yet in its output.
Treat this as bounded raw-prompt behavior. In the coding smoke, TabbyAPI passed
all six cases. IQ4_XS merged integer-adjacent intervals that the contract
required it to keep separate. Both stock servers passed automatic and forced
tool round trips and strict JSON, directly and through LiteLLM. Both rejected
the mid-conversation system message in their stock templates.

## Same-weight GGUF MTP

| Input                | IQ4_XS + MTP cold TTFT (s) | IQ4_XS + MTP warm decode (tok/s) | Accepted draft tokens |
| -------------------- | -------------------------: | -------------------------------: | --------------------: |
| 41-token short probe |                          — |                            39.20 |             313 / 592 |
| 32K                  |                      83.36 |                            43.45 |             331 / 539 |
| 128K                 |                     328.49 |                            40.10 |             371 / 420 |

At 128K, warm wall time fell from 14.61 to 14.35 seconds. Cold time to
first token rose from 250.16 to 328.49 seconds. The short and 32K warm
requests also slowed. The test stopped at 128K for this arm.

## TabbyAPI compatibility and queueing

An overlay on the checkpoint template renders mid-conversation system messages
using the same Qwen role delimiters as the leading system message. It
preserves the messages' order. The patched server passed both tool-selection
paths, structured output, and a system message inserted after an assistant
response, both direct and through LiteLLM Chat Completions. Model readiness
took 64.2 seconds.

A cold 128,023-token request started first. A short request arrived two
seconds later and received its first token after 42.50 seconds. The long
request took 24.44 seconds to its first token and 44.49 seconds overall.
This is the observed head-of-line delay with one active request. Keep it in
mind for interactive use alongside long-context batches.

## Live 256K check after rebalancing the GPUs

On November 18 at 18:42 Pacific, the cluster was serving EXL3 with
`gpu_split: [22, 30]`, one active request, and the same 262,144-token
sequence limit, 270,336-slot Q8 cache and 2,048-token prefill chunk size.
[The GPU budget rollout](https://github.com/agentydragon/ducktape/pull/17885)
addressed a 137K-token prefill OOM on the original split.

Two distinct synthetic inventory-code prompts were sent through the existing
LiteLLM Qwen 256K route using streamed Responses. Each asked for the marker
from the first line followed by a long review. A forced tool-call round trip
passed between the long requests. Both streams retrieved their marker, used
no cached prompt tokens, and ended on the 1,024-token output cap.

| Run | Input tokens | Output tokens | Total tokens | First visible text (s) | End to end (s) | Native decode (tok/s) |
| --- | -----------: | ------------: | -----------: | ---------------------: | -------------: | --------------------: |
| 1   |      261,038 |         1,024 |      262,062 |                  37.42 |          41.64 |                 128.0 |
| 2   |      261,118 |         1,024 |      262,142 |                  36.12 |          40.44 |                 144.8 |

`nvidia-smi` samples every 100 ms across both runs, including the desktop,
showed a minimum of 6,491 MiB free on GPU 0 and 4,785 MiB on GPU 1.
The TabbyAPI pod was ready with zero restarts after the checks.
