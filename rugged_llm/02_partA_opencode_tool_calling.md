# Part A: OpenCode tool calling, offline, Ollama Vulkan + Gemma 4 E2B QAT (2026-10-13)

Backend: Ollama 0.40.2 (Vulkan, Arc iGPU) at `127.0.0.1:11436`, `gemma4:e2b-it-qat`, service default context 131072.
OpenCode 1.19.0 with a scratch config (`tool_call: true`, `permission.edit/bash: allow`, agentplane MCP disabled). No system config touched.
LiteRT-LM was not needed: Ollama's tool calling is usable.

## Result

**Tool calls parse and execute end to end.** The model completed a read, edit, run task in 6 model turns (~97 s of wall time), but it needed a fallback after three failed edits.

Raw probe: 3 of 3 `/v1/chat/completions` requests with one `read_file` tool returned a well-formed `tool_calls` entry (`{"path":"/etc/hostname"}`).

Task: `calc.py` has `sum(xs) / (len(xs) + 1)`. Prompt: "Read calc.py, fix the bug so that average([2, 4, 6]) returns 4.0, then run it with python3 to confirm the output."

| Turn | Tool                          | Outcome               | Decode              | Turn time                                 |
| ---- | ----------------------------- | --------------------- | ------------------- | ----------------------------------------- |
| 1    | `read`                        | ok                    | 15 tok              | ~0 s (after the first 6.8k-token prefill) |
| 2    | `edit`                        | **error** (see below) | 302 tok, 22.3 tok/s | 13.5 s                                    |
| 3    | `edit`                        | **error**             | 393 tok, 21.5 tok/s | 18.3 s                                    |
| 4    | `edit`                        | **error**             | 462 tok, 21.8 tok/s | 21.2 s                                    |
| 5    | `write` (whole file)          | ok, file fixed        | 208 tok, 20.9 tok/s | 9.9 s                                     |
| 6    | `bash` `python3 calc.py`      | ok                    | 85 tok, 21.5 tok/s  | 3.9 s                                     |
| 7    | final text ("Average is 4.0") | ok                    | 398 tok, 21.3 tok/s | 18.6 s                                    |

The final file is correct and `python3 calc.py` prints `4.0`.

Context use: 6.9k to 7.8k tokens per turn.
Follow-up turns processed only 34 to 167 new prompt tokens at 33 to 67 tok/s, so the prompt cache works for follow-up turns.

## Failure modes

1. **Edit tool misuse (3 of 3 attempts failed).** The model copied the `read` output's line-number prefix into `oldString` and `newString` (e.g. `"2:     return sum(xs) / len(xs)"`). The match failed each time and it repeated the same mistake twice before switching to a whole-file `write`. A small model recovers, but each retry costs 13 to 21 s.
2. **Verbose output.** The one-line final answer cost 398 output tokens (18.6 s). Most of the per-turn latency is decode at ~21 tok/s, not prefill.
3. **OpenCode's default prompt is huge in my normal environment.** With my real `$HOME`, OpenCode loads 61 skills (plus duplicate-skill warnings) and the first request is **15.9k tokens**.
   - Ollama had to reprocess the full prompt ("lack of cache data" for SWA models), at 500 tok/s falling to 300 tok/s as depth grew, so that prefill alone takes about 50 s or more.
   - My first run (real `$HOME`, 600 s timeout) produced no output at all; I did not root-cause why it never emitted an event.
   - The second run used an empty `HOME` (`env -i HOME=<scratch>`), which cut the first prompt to 6.9k tokens, and it worked.
   - For benchmarks, keep the harness environment minimal, and report prompt-size effects separately.
4. **Not observed:** the ~20k-context crash. The largest prompt I sent was ~16k tokens, and it was aborted by my own `timeout 60` (visible as a 500 after 57 s), not by a server fault. Part B should push context beyond 20k deliberately.

## Reproduce

```bash
S=<scratch dir>; mkdir -p $S/home $S/xdg $S/work
# $S/oc.json: provider "rugged" (openai-compatible, baseURL http://127.0.0.1:11436/v1),
#   model gemma4:e2b-it-qat with tool_call=true, permission edit/bash=allow, mcp disabled
cd $S/work && env -i PATH="$PATH" HOME=$S/home XDG_DATA_HOME=$S/xdg OPENCODE_CONFIG=$S/oc.json \
  opencode run --pure --format json -m rugged/gemma4:e2b-it-qat "<task>"
```

`--format json` emits one event per step with timestamps and token counts, which is how the per-turn timings above were derived.
Ollama's journal (`prompt eval time` / `eval time` lines) gives the server-side numbers.

## Implication for the repo config

`nix/home/hosts/rugged-opencode.nix` sets `tool_call = false` for `rugged/gemma4:e2b-it-qat`. That comment ("has not been validated") is now out of date: tool calls work, with the caveats above. I made no repo change for this yet.
