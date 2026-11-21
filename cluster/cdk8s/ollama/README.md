# Local inference on wyrm2

## TabbyAPI

TabbyAPI starts with Qwen3.8 Flash-Next EXL3 2.68-bit and can switch to the
abliterated 2.50-bit checkpoint by request. Both use the two 5090s, a configured
262,144-token context, and one active generation stream. Switching checkpoints
waits for the current request to finish and reloads weights from NVMe.
Matching prompt prefixes can be restored from the NVMe cache after the switch.

TabbyAPI uses an 8 GiB pinned RAM prefix cache and a 16 GiB persistent NVMe
prefix cache on `/games/tabbyapi-prefix-cache`. Kubernetes requests 20 GiB of
RAM for the pod, including the pinned cache. The NVMe budget is enforced by
TabbyAPI and shared across both checkpoints. The cache stores prompt token
IDs and recurrent state from serving traffic. The dedicated local volume
retains those files across pod replacement. Keep its directory accessible
only to the host owner and TabbyAPI's UID 0. See the
[211K-prefix measurements](../../docs/inference/runs/2026-11-19_tabby_prefix_cache/README.md)
and [directory setup](prefix-cache-probe.md#prepare-the-gitops-rollout).

With the original 28/30 GiB loader budgets, a 137K-token prefill ran out of
memory with about 200 MiB free on GPU 0 and 11.3 GiB free on GPU 1. The
warm TabbyAPI process used 29.97 GiB and 20.03 GiB, respectively. Allowing
GPU 0 another 1 GiB for desktop growth and dividing the remaining headroom
evenly gives warm-use targets of 23.9 GiB and 26.1 GiB, or about 6 GiB to
move from GPU 0. The 22/30 GiB loader budgets use that estimate to guide
placement. After rollout, measure warm and peak VRAM on both cards while
completing a cold prompt plus generation near the 256K limit through LiteLLM.
Repeat with a fresh prefix after mixed requests.

The LiteLLM compatibility IDs
`ollama/{oai,olm}-chat/qwen3.8-flash-next-iq4xs-{128k,256k}` all use this
deployment and select the stock checkpoint. Select
`tabbyapi/oai-chat/qwen3.8-flash-next-abliterated` in LiteLLM or
**Qwen3.8 Flash-Next Abliterated (EXL3 2.50 bpw) — TabbyAPI**
in Agentplane staging's Codex and central sampler pickers. Codex uses
LiteLLM's Responses bridge and the central sampler uses Chat Completions.
Gatus probes the private
`/v1/models` endpoint. LiteLLM handles client authentication.

The static local volumes mount the [staged checkpoints](#tabbyapi-checkpoint-staging)
on the wyrm2 NVMe read-only. The ConfigMap overlays a chat template that supports
mid-conversation system messages and maps the established `high` effort to
Qwen's `xhigh`.

The initial deployment took 55 seconds to load the model. The first
authenticated request took 37 seconds while the engine compiled kernels.
During that request the health endpoint timed out, and Kubernetes briefly
removed the Service endpoint. TabbyAPI's built-in warmup runs during model
load. The readiness probe also tolerates six consecutive timeouts to cover
remaining first-use compilation. After a restart, watch the initial request
and confirm the Service endpoint stays ready.

Ollama is scaled to zero. Its direct API, embeddings, and other model routes
are offline. To restore Ollama through GitOps, first revert the TabbyAPI
deployment and LiteLLM bindings and wait for the TabbyAPI Pod to stop.
Then restore the Ollama Deployment to one replica. Its existing claims,
registry, and completed setup Job support that rollback.

### TabbyAPI checkpoint staging

Checkpoint files live under `/games/llm-models/` on `wyrm2`.

| Directory                     | Use                                                   | Source                                                                                                                                                           |
| ----------------------------- | ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `qwen38-exl3-268`             | Serving, 2.68 bpw self-calibrated, 8-bit n-gram table | [turboderp revision `d660113`](https://huggingface.co/turboderp/Qwen3.8-Flash-Next-exl3/tree/d660113325e1eba20e760a9063580139bdbdfc22)                           |
| `qwen38-exl3-250-abliterated` | Serving on demand, 2.50 bpw, 3-bit n-gram table       | [ghost-actual revision `1354024`](https://huggingface.co/ghost-actual/Qwen3.8-Flash-Next-Abliterated-EXL3-2.50bpw/tree/135402464a8d351ca6ecb6969f2a6f4061681a24) |

The candidate quantizes [OrcaRouter's abliterated BF16 checkpoint](https://huggingface.co/orcarouter/Qwen3.8-Flash-Next-Uncensored).
The ablation is in the model weights. The download includes the MTP weights and
`ngram_embedding.safetensors`. Full-context serving and MTP throughput on the
cluster image still need measurement.

To stage the candidate from a Ducktape checkout on `wyrm2`, use the Hugging
Face CLI from the repository's Nixpkgs pin.

```sh
nixpkgs_revision=$(jq -er '.nodes.nixpkgs.locked.rev' flake.lock)
nix shell "github:NixOS/nixpkgs/$nixpkgs_revision#python3Packages.huggingface-hub" \
  --command ionice -c 3 nice -n 10 bash cluster/cdk8s/ollama/stage_tabbyapi_checkpoint.sh
```

The wrapper uses `hf download` to resume partial downloads and `hf cache verify`
to check the pinned snapshot before renaming the staging directory. Hugging Face
locks individual files. The wrapper holds a separate lock through the rename so
overlapping runs can safely share the staging directory. Rerunning the wrapper
verifies the existing checkpoint. The pinned CLI reports its own `.cache/huggingface/`
resume metadata as extra files. To restore the serving checkpoint onto a replacement
disk, stage its pinned snapshot with the n-gram table and verify its weight hashes
before starting TabbyAPI.
See [the declarative-staging TODO](TODO.md).

## Ollama provisioning

`app.py` renders `setup-gpt-oss-v2.sh.j2` using the source declarations in
`model_catalog/ollama.py` and packages the result as `setup-gpt-oss-v2.sh`.
Those declarations also feed LiteLLM routing; provisioning does not depend on
LiteLLM's route roster. Other ConfigMap inputs beside `app.py` are
`link-ssd-models.sh` and the two shard manifests (`gpt-oss-scripts`), and the nginx
bearer proxy's configuration (`auth-proxy`). `qwen38-chat-template.jinja` is
not deployed; it is the derivation input below.

## Deferred provisioning cleanup

TODO([#9233](https://github.com/agentydragon/ducktape/issues/9233)): keep the current
shell/Jinja executor for now. Revisit these options separately from the roster work:

- A small one-shot image using the [official Python client](https://github.com/ollama/ollama-python):
  reuse its create/pull calls and typed progress instead of writing HTTP/JSON parsing.
  Generate configuration data, not executable source; check terminal success explicitly.
- An [operator for an existing Ollama server](https://github.com/dmk/ollama-operator),
  later: verify custom model/alias support and retention before adopting its pull/delete
  lifecycle. Broader serving operators are a separate deployment decision.
- [Ollama Helm](https://github.com/otwld/ollama-helm) supports declarative pull/create,
  but uses templated shell in a serving-container startup hook. NixOS and Terraform
  partial alternatives, plus the source-inspection findings, are recorded in the issue.

No replacement is selected. Any image/Job change needs an explicit version/rerun plan
for the immutable setup Job and the shared-volume constraint described below; no
incidental provisioning, pruning, storage changes or consumer reactivation.

## Direct bearer token

ESO's `Password` generator creates `ollama/ollama-direct-token`, retained and not
periodically rotated (`refreshInterval: 8760h`). Reflector copies it to
`claude-sandbox`. Nginx reads it from an environment variable, so Reloader restarts
Ollama when it changes; the Deployment uses `Recreate`, so a rotation interrupts
service. Clients that cached the previous token must fetch the new value from
`claude-sandbox/ollama-direct-token:token`.

## SSD weights alongside HDD models

The `models` PVC (`piraeus-hdd-local`) is Ollama's writable `/models` registry.
`qwen38-iq4` (`piraeus-ssd-local`) holds the `UD-IQ4_XS` directory, mounted read-only
at `/ssd-models`. Both are strict-local claims on wyrm2's Piraeus pools, which pins
the consumers to wyrm2; the startup file checks make a missing model fail rather than
serve an empty directory.

The init container links digest-named blobs in `/models/blobs` to the three original
SSD shards and the derived template shard. `qwen38-ssd-shards.tsv` pins the original
SHA256 digests and lengths from the
[verified download recipe](../../docs/inference/runs/2026-09-26_qwen38_capacity/README.md).
Startup checks lengths, not hashes, and refuses to replace an existing file or a
different link. The setup Job registers `qwen3.8-flash-next-iq4xs:latest` through
`/api/create` without copying the weights. Ollama 0.34.4 calls `chtimes` on imported
blobs, so registration fails against the serving API's read-only mount: the Job runs
its own loopback-only Ollama API as a native sidecar, with the same HDD registry and a
writable mount of only the IQ4 directory, no GPUs and no inference. Kubernetes stops
the sidecar when setup finishes. Small manifests and other models stay on HDD; Ollama
has one model root, so this is filesystem-managed placement, not a native per-model
storage tier.

The setup Job and the serving Pod both mount `models` on wyrm2. If the Job's Pod
cannot mount it beside the serving Pod (the retired `lvm-proxmox-hdd` claim refused
with `verifyMount: device already mounted`), pause the serving Deployment through
GitOps, wait for the Job to complete, then restore one serving replica.
The completed Job has no TTL: Flux keeps it until the Job's version is bumped, which
is also how to rerun registration. It opts out of Reloader, which would delete its
running Pod on a scripts ConfigMap update.

`OLLAMA_NOPRUNE=true` keeps startup garbage collection from unlinking the external
blobs before registration, so unused blobs need deliberate cleanup; do not remove SSD
files while a manifest references them.

## GPU placement

One loaded model and one inference slot avoid concurrent KV-cache growth.
`LLAMA_ARG_FIT_TARGET=4096,2048` leaves runtime allocation room on both GPUs plus
desktop headroom; 2/0 GiB passed short requests but hit CUDA OOM on GPU1 during a
145K-token prompt at 256K context. These are placement targets, not exclusive
reservations, and differ from the host experiments' 8/2 GiB: record actual placement
and headroom when comparing throughput. Vulkan discovery is off and CUDA uses PCI bus
order. Startup logs sort GPUs by free memory and do not establish runner order; inspect
the runner's `CUDA_VISIBLE_DEVICES` and physical GPU headroom after loading. Stop
exclusive host experiments before resuming this Deployment. Serving acceptance,
throughput and tool-call results:
[2026-09-27 run](../../docs/inference/runs/2026-09-27_ollama_ssd/README.md).

LiteLLM derives the Qwen compatibility IDs and key allowlists from the shared
model roster. When bound to Ollama, the 256K routes use
`qwen3.8-flash-next-iq4xs-256k:latest`, an alias sharing all weight blobs with
`num_ctx=262144`: Ollama's OpenAI-compatible API ignores native `options.num_ctx`, so
the alias makes the context effective on both wires. A short prompt does not
demonstrate usable long-context quality; inspect the loaded runner's context.

## Claude system reminders in Qwen3.8

Claude Code can send a system reminder after a user message, which the pinned Unsloth
GGUF template rejects. `qwen38-chat-template.jinja` is the model's original
`tokenizer.chat_template` with one branch changed: a later `system` or `developer`
message becomes a ChatML system turn at the same position (Qwen's template already maps
leading developer content into one). `patch_qwen38_template.py` pins the original
template's SHA256.

The 10.9 MB first GGUF shard holds this metadata. Its derived copy lives in
`qwen38-iq4` under `agentplane-midturn/` and must exist **before** the `setup-gpt-oss`
Job runs; the two large weight shards and all source files stay untouched. To
regenerate it, run the patcher against the claim's mount, here `$ssd`:

```bash
bb run //cluster/cdk8s/ollama:derive_qwen38_template -- \
  "$ssd/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" \
  "$PWD/cluster/cdk8s/ollama/qwen38-chat-template.jinja" \
  "$ssd/agentplane-midturn/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" \
  8681e217aad3be934fd9542709bf44123c4b569cb7906c01a2a383ac79c7c05f
```

The patcher verifies the source hash, the exact original template, the sole intended
edit and the derived hash, and refuses to replace a different output. The Job links the
derived shard as a new blob under the original first split filename; the other two
filenames and digests stay fixed. The 256K alias inherits the 128K model's template.
