# Proposal: Home Manager vLLM user service on wyrm2

Updated 2026-10-28.

## Status

This remains an **unimplemented proposal**. The current `nix/home` tree has an
OpenCode vLLM provider profile, but no Home Manager vLLM systemd-user service.
The July 17 Kubernetes run and the host launcher below validate vLLM
configurations; neither is this proposed Home Manager service. The live cluster
read on 2026-10-28 found Ollama 1/1 and no vLLM Deployment or Pod. That does not
check for an unmanaged host process on wyrm2.

The idea remains open, but service implementation is not in the active
capability-first evaluation queue in <PLAN.md>. Revisit it after model/runtime
selection establishes a need for a managed host service.

## Validated reference configuration

The July 17 E1 run, <runs/2026-07-17_e1_qwen3coder_awq/README.md>, successfully
served Qwen3-Coder-30B-A3B AWQ through Kubernetes vLLM with tensor parallelism 2,
262,144 configured context, FP8 KV cache, GPU memory utilization 0.90, and
`--max-num-seqs 32`. A 255,669-token request completed; the run also records
needle and tool-call probes.

The model files identify as `compressed-tensors`; vLLM auto-detects that format.
Do not pass `--quantization awq`, which made the earlier launch fail. The exact
measured configuration and limitations are in the run record.

The current host launcher is [`x/local_llm/start-vllm-awq.sh`](../../../x/local_llm/start-vllm-awq.sh).
It uses Podman with NVIDIA CDI, the `/wyrmhdd/huggingface` cache, FP8 KV, and a
262,144 default context. This host experiment is separate from the unimplemented
Home Manager service proposal.

## Remaining design work

If a managed host service is still useful after the active model/runtime work:

- Add and import a Home Manager user unit for wyrm2, using the host's current
  Podman and NVIDIA CDI setup rather than the old `/usr/bin/docker` draft.
- Decide whether the service should start automatically and how it coordinates
  GPU ownership with the Ollama workload.
- Keep model/cache paths and the validated launch arguments explicit, then verify
  service start, stop, restart, and API readiness on wyrm2.

No service is declared or live-verified by this proposal.
