# GPT-OSS on the local host

The host launchers are [`start-ollama.sh`](start-ollama.sh) and
[`start-vllm.sh`](start-vllm.sh). Their defaults are
source configuration, not confirmation that the model files are currently cached:
Ollama uses model `gpt-oss` under `/wyrmhdd/ollama-models`; vLLM uses
`openai/gpt-oss-20b` under `/wyrmhdd/huggingface`. Each launcher documents its
model, context, and port overrides.

Read [`start-ollama.sh`](start-ollama.sh) before changing how Ollama is launched:
it starts with a clean non-Nix environment so the host linker can find
`libcuda.so.1`. Its comments also record why Podman 4.x CDI GPU passthrough can
silently fall back to CPU.

The optional Codex profile is `gpt-oss-ollama`, enabled through
`ducktape.codex.localModels.enable`; see the [Nix Codex module](../../../nix/home/codex/default.nix).
Current backend research lives in the [inference hub](../../../cluster/docs/inference/README.md),
including its [backend comparison](../../../cluster/docs/inference/backend_comparison.md).

Issue [vLLM #28262](https://github.com/vllm-project/vllm/issues/28262) recorded a
GPT-OSS multi-turn Responses API problem; earlier notes also reported missing
tool calls when streaming. These are historical, version-sensitive reports, not
claims about current releases. Still-untried ideas include the
[Responses proxy](https://github.com/chutesai/responses-proxy), SGLang, and
TensorRT-LLM.

The two-GPU GPT-OSS-120B experiment is recorded in the
[2026-07-17 E7 run](../../../cluster/docs/inference/runs/2026-07-17_e7_gptoss120b/README.md).
