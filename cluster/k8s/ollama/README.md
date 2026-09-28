# Ollama direct bearer token

`cluster/cdk8s/ollama/app.py` uses ESO's `Password` generator to
create `ollama/ollama-direct-token`. The generated target is retained and is
not periodically rotated (`refreshInterval: 8760h`).

Reflector copies the Secret to `claude-sandbox`. Reloader restarts Ollama when
the token changes because nginx reads it from an environment variable. The
Deployment uses `Recreate`, so a deliberate rotation includes a service
interruption. Clients that cached the previous bearer token must fetch the new
value from `claude-sandbox/ollama-direct-token:token`.

## SSD weights alongside HDD models

The existing `llm-models` PVC remains Ollama's writable `/models` registry on HDD.
`qwen38-iq4-ssd` is a second, statically bound PVC: its retained local PV exposes
wyrm2's existing `/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS`
directory read-only at `/ssd-models`. It does not allocate, format or resize a disk,
and does not use OpenEBS. The advertised 90Gi capacity is not an enforced quota.
Node affinity pins the consumer to wyrm2. The model-specific path and startup file
checks cause a missing SSD/model to fail rather than silently use an empty directory.

The init container creates digest-named symlinks in `/models/blobs` to the
three original SSD shards and the derived template shard. `qwen38-ssd-shards.tsv`
pins the original SHA256 digests and lengths from the
[verified download recipe](../../docs/inference/runs/2026-09-26_qwen38_capacity/README.md).
Startup checks lengths and refuses to replace an existing file or a different link;
it does not rehash 94 GB on every Pod restart. The setup Job calls `/api/create` to
register `qwen3.8-flash-next-iq4xs:latest` without copying the weights. Ollama 0.34.4
calls `chtimes` on imported blobs, so registration against the serving API fails on
the read-only mount. The short-lived setup Job therefore runs its own loopback-only
Ollama API as a native sidecar, with the same HDD registry and a writable mount of
only the IQ4 directory. It requests no GPUs and performs no inference. Once setup
finishes Kubernetes stops that sidecar; the serving Deployment stays read-only.
Small manifests
and other models stay on HDD. Ollama has one model root; this is filesystem-managed
placement, not a native per-model storage tier setting.

`OLLAMA_NOPRUNE=true` prevents startup garbage collection from unlinking the external
blobs before registration. Unused blobs therefore require deliberate cleanup; do not
remove SSD files while a manifest references them. The SSD PV/PVC have Flux pruning
disabled, the PV uses `Retain`, and the owning Kustomization uses `Orphan` on deletion.
The existing HDD PVC is not rebound or migrated.

The SSD is the separate host-managed ext4 model disk, not the SSD LVM volume group.
The [historical thin-pool incident](../../docs/lessons_learned/2026_07_17_lvm_thinpool_exhaustion_emergency_ro.md)
was physical pool exhaustion despite VG free space. Nix now declares auto-extension
mitigations; their live coverage of new pools is not established by this change.

One loaded model and one inference slot avoid concurrent KV-cache growth.
`LLAMA_ARG_FIT_TARGET=4096,2048` leaves runtime allocation room on both GPUs and
additional desktop-GPU headroom. The tested 2/0 GiB setting passed short requests
but hit CUDA OOM on GPU1 during a 145K-token prompt at 256K context. These are
placement targets, not exclusive reservations. The 4/2 GiB configuration passed a
145K-token prompt and a 1,024-token continuation at 256K context; see the
[serving acceptance results](../../docs/inference/runs/2026-09-27_ollama_ssd/README.md).
This differs from the host experiments' 8/2 GiB targets;
record actual placement and headroom when comparing throughput. Vulkan discovery is
disabled and CUDA uses PCI bus ordering. Startup logs sort GPUs by free memory and
do not establish runner order: inspect the runner's `CUDA_VISIBLE_DEVICES` and
physical GPU headroom after loading. Ollama retains
its 40Gi RAM limit, Q8 KV and 128K context. Stop exclusive host experiments before
resuming this Deployment.

LiteLLM derives both routes and key allowlists from the shared model roster:

- `ollama/oai-chat/qwen3.8-flash-next-iq4xs-128k`
- `ollama/olm-chat/qwen3.8-flash-next-iq4xs-128k`
- `ollama/oai-chat/qwen3.8-flash-next-iq4xs-256k`
- `ollama/olm-chat/qwen3.8-flash-next-iq4xs-256k`

The 256K routes use `qwen3.8-flash-next-iq4xs-256k:latest`, an Ollama alias sharing
all weight blobs but setting `num_ctx=262144`. Ollama's OpenAI-compatible API ignores
native `options.num_ctx`, so the alias makes context selection effective on both
wires. Inspect the loaded runner context; a short prompt alone does not demonstrate
usable long-context quality.

After merge, verify the PV/PVC binding, read-only mount and blob-link targets, setup
Job success, `/api/show` and `/api/ps`. Then run serial direct-Ollama and authenticated
LiteLLM generation requests at both 128K and 256K configured context, recording load time separately
from warm decode throughput and checking GPU/host memory headroom. Include a tool-call
round trip. The earlier IQ4 llama.cpp run averaged 40.79 decode tokens/s; the earlier
HDD Ollama path was 0.056–1.44 tokens/s. This storage change is not yet evidence that
Ollama reaches the SSD reference speed; record the live measurements before claiming
that acceptance criterion is met.

## Claude system reminders in Qwen3.8

Claude Code can send a system reminder after a user message. The pinned
Unsloth GGUF template rejects that sequence. `qwen38-chat-template.jinja` is
the model's original `tokenizer.chat_template` with one branch changed: a
later `system` or `developer` message becomes a ChatML system turn at the same
position. Qwen's template already maps leading developer content into a system
turn. The original template SHA256 is pinned in
`../../cdk8s/ollama/patch_qwen38_template.py`.

The first GGUF shard is 10.9 MB and holds this metadata. On wyrm2, generate
its derived copy **before** deploying the `setup-gpt-oss-v8` Job. The two large
weight shards and all source files remain untouched:

```bash
ssd=/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS
bb run //cluster/cdk8s/ollama:derive_qwen38_template -- \
  "$ssd/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" \
  "$PWD/cluster/k8s/ollama/qwen38-chat-template.jinja" \
  "$ssd/agentplane-midturn/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf" \
  8681e217aad3be934fd9542709bf44123c4b569cb7906c01a2a383ac79c7c05f
```

The patcher verifies the source hash, exact original template, sole intended
template edit, and derived hash. It refuses to replace a different output.
The Job links the derived shard as a new Ollama blob and passes its digest
under the original first split filename; the other two split filenames and
digests stay fixed. The 256K alias inherits the 128K model's template.
The setup Job opts out of Reloader because a scripts ConfigMap update would
delete its running Pod. Change the Job's explicit version to rerun registration.
