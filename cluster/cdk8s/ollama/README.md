# Ollama

`app.py` renders `setup-gpt-oss-v2.sh.j2` using the source declarations in
`model_catalog/ollama.py` and packages the result as `setup-gpt-oss-v2.sh`.
Those declarations also feed LiteLLM routing; provisioning does not depend on
LiteLLM's route roster. Other ConfigMap inputs beside `app.py` are
`link-ssd-models.sh` and the two shard manifests (`gpt-oss-scripts`), and the nginx
bearer proxy's configuration (`ollama-auth-proxy`). `qwen38-chat-template.jinja` is
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

The `llm-models` PVC remains Ollama's writable `/models` registry on HDD.
`qwen38-iq4-ssd` is a second, statically bound PVC: its retained local PV exposes
wyrm2's existing `/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS`
directory read-only at `/ssd-models`. It does not allocate, format or resize a disk,
and does not use OpenEBS; the advertised 90Gi is not an enforced quota. Node affinity
pins the consumer to wyrm2, and the model-specific path and startup file checks make a
missing SSD or model fail rather than serve an empty directory.

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

The `lvm-proxmox-hdd` CSI rejects mounting the `llm-models` PVC in the serving Pod and
the registration Job at once (`verifyMount: device already mounted`). To register,
pause the serving Deployment through GitOps, wait for its Pod to terminate and the
versioned Job to complete, then restore one serving replica. The static SSD PV has no
such restriction.
The completed Job has no TTL: Flux keeps it until the Job's version is bumped, which
is also how to rerun registration. It opts out of Reloader, which would delete its
running Pod on a scripts ConfigMap update.

`OLLAMA_NOPRUNE=true` keeps startup garbage collection from unlinking the external
blobs before registration, so unused blobs need deliberate cleanup; do not remove SSD
files while a manifest references them. The SSD PV/PVC have Flux pruning disabled, the
PV uses `Retain`, and the owning Kustomization orphans on deletion. The HDD PVC is not
rebound or migrated.

The SSD is wyrm2's separate host-managed ext4 model disk, not the SSD LVM volume group.
The [thin-pool incident](../../docs/lessons_learned/2026_07_17_lvm_thinpool_exhaustion_emergency_ro.md)
was physical pool exhaustion despite VG free space; Nix declares auto-extension
mitigations, whose live coverage of new pools is unverified.

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

LiteLLM derives its `ollama/{oai,olm}-chat/qwen3.8-flash-next-iq4xs-{128k,256k}` routes
and key allowlists from the shared model roster. The 256K routes use
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

The 10.9 MB first GGUF shard holds this metadata. On wyrm2, generate its derived copy
**before** deploying the `setup-gpt-oss-v8` Job; the two large weight shards and all
source files stay untouched:

```bash
ssd=/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS
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
