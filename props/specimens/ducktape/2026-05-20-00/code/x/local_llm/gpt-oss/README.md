# GPT-OSS Local Setup

GPT-OSS is OpenAI's open-weight reasoning model with native thinking and tool calling.

## Model Specs

| Model        | Total Params | Active Params | Context | VRAM     |
| ------------ | ------------ | ------------- | ------- | -------- |
| GPT-OSS-20B  | 21B          | ~2B (MoE)     | 128K    | ~14GB    |
| GPT-OSS-120B | 117B         | ~5B (MoE)     | 128K    | ~56-80GB |

## Backends

### Ollama (Recommended for quick start)

Already downloaded at `/wyrmhdd/ollama-models/`:

- `gpt-oss:20b` - Full 128K context
- `gpt-oss:20b-32k` - Reduced context

```bash
# Start Ollama
ollama serve

# Test
curl http://localhost:11434/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-oss:20b", "messages": [{"role": "user", "content": "Hello"}]}'
```

### vLLM (Better for throughput)

Model downloaded at `/wyrmhdd/huggingface/hub/models--openai--gpt-oss-20b/`.

```bash
# Start vLLM (uses start-vllm.sh)
./start-vllm.sh

# Test
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-oss-20b", "messages": [{"role": "user", "content": "Hello"}]}'
```

vLLM flags for tool calling (already set in start-vllm.sh):

- `--enable-auto-tool-choice` - Enables automatic tool selection
- `--tool-call-parser openai` - Uses OpenAI format for tool calls

## Using with Codex CLI

Codex CLI is OpenAI's coding agent that supports local models.

```bash
# Install
npm install -g @openai/codex

# With Ollama
CODEX_OSS_BASE_URL=http://localhost:11434/v1 codex --oss --model gpt-oss:20b

# With vLLM
CODEX_OSS_BASE_URL=http://localhost:8000/v1 codex --oss --model gpt-oss-20b

# Or use profiles configured in ~/.codex/config.toml
codex --profile gpt-oss          # vLLM (chat completions)
codex --profile gpt-oss-ollama   # Ollama
```

The Nix config at `nix/home/codex/default.nix` sets up profiles for both backends.

## Known Issues

1. **vLLM Responses API broken for multi-turn**: vLLM's `/v1/responses` endpoint
   mishandles GPT-OSS harmony format on turn 2+ (reasoning_text parsing fails).
   Tracked in [vllm#28262](https://github.com/vllm-project/vllm/issues/28262).
   Workaround: use Chat Completions API (`wire_api = "chat"` in Codex config).
   Single-turn `/v1/responses` calls work fine.
2. **vLLM streaming bugs**: Tool calls may be missing when `stream=True`.
3. **Ollama tool calling**: May have different behavior than vLLM.

## TODO

- Try [chutesai/responses-proxy](https://github.com/chutesai/responses-proxy)
  as a Responses→Chat Completions translator in front of vLLM.
- Try SGLang or TensorRT-LLM as alternative backends (may have working Responses API).
- Try GPT-OSS-120B with `--tensor-parallel-size 2` across both RTX 5090s (needs ~56-80GB, have 64GB).
- Re-test vLLM Responses API after vllm#28262 is fixed.

## References

- [OpenAI Cookbook: Run GPT-OSS with vLLM](https://cookbook.openai.com/articles/gpt-oss/run-vllm)
- [vLLM GPT-OSS Recipes](https://docs.vllm.ai/projects/recipes/en/latest/OpenAI/GPT-OSS.html)
- [GPT-OSS Model Card](https://huggingface.co/openai/gpt-oss-20b)
- [Codex CLI Docs](https://developers.openai.com/codex/cli/)
