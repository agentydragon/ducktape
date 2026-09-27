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

The init container creates three digest-named symlinks in `/models/blobs` to the
SSD shards. `qwen38-ssd-shards.tsv` pins their SHA256 digests and lengths from the
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
`LLAMA_ARG_FIT_TARGET=2048,0` requests the operator-approved 2 GiB desktop-GPU
headroom and no additional placement margin on GPU1. These are placement targets,
not exclusive reservations. This differs from the host experiments' 8/2 GiB targets;
record actual placement and headroom when comparing throughput. Vulkan discovery is
disabled and CUDA uses PCI bus ordering: mixed-backend duplicate removal was observed
to list PCI GPU1 before GPU0, which could reverse the runner's margin mapping. Verify
the runner's `CUDA_VISIBLE_DEVICES` and physical GPU headroom after loading. Ollama retains
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
