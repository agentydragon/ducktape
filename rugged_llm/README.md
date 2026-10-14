# Rugged local-LLM evaluation

Findings from verifying and benchmarking local inference on `rugged` (Core Ultra 7 268V, Arc 130V/140V iGPU, Lunar Lake NPU, 30 GiB shared RAM), 2026-10-13.
Goal: an agent harness that runs fully offline on rugged, later exposed to the cluster through LiteLLM.

| Doc                                 | Content                                                                             |
| ----------------------------------- | ----------------------------------------------------------------------------------- |
| <01_verify_existing_setup.md>       | What actually runs today (Ollama Vulkan, LiteRT-LM, disabled NPU service, OpenCode) |
| <02_partA_opencode_tool_calling.md> | OpenCode tool calling offline against Ollama + Gemma 4 E2B                          |
| <03_partB_model_benchmarks.md>      | Speed and memory of Qwen3 4B/8B, Qwen2.5-Coder 7B, Gemma 4 E4B                      |
| <04_partC_npu_driver_research.md>   | NPU driver bump and Level Zero setup (research only)                                |

Not done yet: coding-eval quality, cluster/LiteLLM exposure, NPU inference.

## How to run the models

All commands run as a normal user on rugged. Nothing here changes NixOS config.

### The system Ollama (iGPU, Vulkan)

`ducktape.localLlm.ollamaUpstream` runs Ollama on `127.0.0.1:11436` (`nix/nixos/hosts/rugged/local_llm_arc.nix`). It serves `gemma4:e2b-it-qat`.

```bash
curl -s 127.0.0.1:11436/api/chat -d '{"model":"gemma4:e2b-it-qat","stream":false,"think":false,
  "messages":[{"role":"user","content":"Reverse a string in Python."}],"options":{"num_ctx":8192}}'
```

### Trying other models without touching the service

Run a second, user-level Ollama on another port with a scratch model dir, and pull the candidates into it.
Models are fetched through the environment's proxy, and the system service cannot do this.

```bash
export OLLAMA_HOST=127.0.0.1:11500 OLLAMA_MODELS=/path/to/scratch/models \
       OLLAMA_IGPU_ENABLE=1 OLLAMA_CONTEXT_LENGTH=32768 OLLAMA_KEEP_ALIVE=2m
ollama serve &        # same Ollama build as the service (0.40.2, Vulkan)
ollama pull qwen3:4b-instruct-2507-q4_K_M
ollama pull qwen2.5-coder:7b
ollama pull qwen3:8b
ollama pull gemma4:e4b
```

The iGPU memory is shared with the system service, so unload its model first (it idles out after about 5 minutes) and run one model at a time.

### Benchmark script

Measures cold load, prefill and decode speed, and host RSS at several prompt sizes with a given context.
The prompt is random-word filler, so it measures speed and not quality.

```python
import json, random, subprocess, sys, time, urllib.request

H = "http://127.0.0.1:11500"
words = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon".split()
random.seed(1)


def post(path, body):
    req = urllib.request.Request(H + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=1800))


def rss_gb():
    out = subprocess.run(
        "ps -eo rss,args | grep -E 'llama-server|ollama runner' | grep -v grep | awk '{s+=$1} END{print s/1048576}'",
        shell=True, capture_output=True, text=True,
    ).stdout.strip()
    return float(out or 0)


# usage: bench.py MODEL NUM_CTX PROMPT_TOKENS[,PROMPT_TOKENS...]
model, ctx, sizes = sys.argv[1], int(sys.argv[2]), [int(x) for x in sys.argv[3].split(",")]
post("/api/generate", {"model": model, "keep_alive": 0})
time.sleep(2)
t = time.time()
post("/api/chat", {"model": model, "stream": False, "think": False, "keep_alive": "5m",
                   "messages": [{"role": "user", "content": "hi"}], "options": {"num_ctx": ctx, "num_predict": 4}})
loaded = json.load(urllib.request.urlopen(H + "/api/ps"))["models"][0]
print(f"cold_load_s={time.time() - t:.1f} size_vram_gib={loaded['size_vram'] / 2**30:.2f} ctx={loaded['context_length']}")
for n in sizes:
    prompt = "Summarize the following text in one sentence.\n\n" + " ".join(random.choice(words) for _ in range(int(n * 0.75)))
    t = time.time()
    d = post("/api/chat", {"model": model, "stream": False, "think": False, "keep_alive": "5m",
                           "messages": [{"role": "user", "content": prompt}],
                           "options": {"num_ctx": ctx, "num_predict": 128, "seed": 1}})
    print(f"prompt={d['prompt_eval_count']} prefill_tps={d['prompt_eval_count'] / d['prompt_eval_duration'] * 1e9:.1f} "
          f"decode_tps={d['eval_count'] / d['eval_duration'] * 1e9:.1f} wall_s={time.time() - t:.1f} rss_gb={rss_gb():.2f}")
post("/api/generate", {"model": model, "keep_alive": 0})
```

```bash
python3 -I bench.py qwen3:4b-instruct-2507-q4_K_M 32768 500,4000,12000,24000
```

A 24k-token prompt takes about 5 minutes to prefill, so run long sizes in the background.

### OpenCode with tool calling, offline

Use a scratch config (do not edit the Home Manager one) and an empty `HOME`.
With the real `$HOME`, OpenCode loads dozens of skills and the first prompt is about 16k tokens, which takes about a minute to prefill on this iGPU.

```bash
cat > oc.json <<'JSON'
{"$schema":"https://opencode.ai/config.json",
 "permission":{"edit":"allow","bash":"allow"},
 "provider":{"rugged":{"npm":"@ai-sdk/openai-compatible","name":"Rugged local Ollama",
   "options":{"baseURL":"http://127.0.0.1:11436/v1"},
   "models":{"gemma4:e2b-it-qat":{"name":"Gemma4 E2B","tool_call":true,"reasoning":false,
     "limit":{"context":32768,"output":4096}}}}}}
JSON
mkdir -p scratch-home scratch-xdg work && cd work
env -i PATH="$PATH" HOME=$PWD/../scratch-home XDG_DATA_HOME=$PWD/../scratch-xdg OPENCODE_CONFIG=$PWD/../oc.json \
  opencode run --pure --format json -m rugged/gemma4:e2b-it-qat \
  "Read calc.py, fix the bug so that average([2, 4, 6]) returns 4.0, then run it with python3."
```

`--format json` prints one event per step with timestamps and token counts. Ollama's journal (`journalctl -u ollama`) has the server-side `prompt eval time` / `eval time` lines.

### LiteRT-LM (GPU) needs a Vulkan loader on the library path

In a plain shell `litert-lm --backend=gpu` fails with "libvulkan.so.1: cannot open shared object file".
Point it at a 64-bit `vulkan-loader` and the Intel ICD (a 32-bit loader fails with `ELFCLASS32`):

```bash
LD_LIBRARY_PATH=/nix/store/<hash>-vulkan-loader-<ver>/lib \
VK_ICD_FILENAMES=/run/opengl-driver/share/vulkan/icd.d/intel_icd.x86_64.json \
  litert-lm benchmark gemma4-e2b-it --backend=gpu --prefill-tokens=512 --decode-tokens=128
```

### NPU Level Zero smoke test

The NPU service is disabled and the driver is not installed in the running system.
The 1.28.0 validation suite can still be pointed at the driver's store paths for a read-only check:

```bash
LD_LIBRARY_PATH=<level-zero-1.28.5>/lib:<intel-npu-driver-1.28.0>/lib <intel-npu-driver-1.28.0-validation>/bin/npu-umd-test
```

See <04_partC_npu_driver_research.md> for what the output means.
