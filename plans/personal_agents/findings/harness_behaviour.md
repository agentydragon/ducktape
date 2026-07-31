# Harness behaviour

Findings are numbered in discovery order across the whole programme and cited by
number from cluster manifests, so the IDs are stable and non-contiguous here.
Index of all findings: [README.md](README.md).

## F5. The workspace git repo is real but never used

Confirmed empirically, matching the source reading (findings.md, C8). In a
live workspace the repo exists but `HEAD` is unborn and the bootstrap files are
untracked:

```text
?? IDENTITY.md
branch: fatal: ambiguous argument 'HEAD': unknown revision or path not in the working tree
commits: fatal: ambiguous argument 'HEAD'...
```

So nothing ever commits. Worth noting the agent _noticed_ on its own: asked to
save a memory it wrote the file and then reported "the changes are staged, but
not committed because Git has no configured `user.name`/`user.email`". Setting
those two values is all that stands between this and a git-backed memory store —
relevant to K1/K2 and to the durable-memory question generally.

## F9. Memory embedding search was never configured, and the fix is a provider swap

`openclaw memory index` failed with:

```text
No API key found for provider "openai". Auth store: .../openclaw-agent.sqlite
```

`agents.defaults.memorySearch.provider` defaults to `"openai"` and looks for auth
under a provider literally named `openai`. Ours is named `litellm-subscription`
and serves six chat models and no embedding model, so there was never a backend to
index against. This is a **config gap, not a bug**, and it never affected recall:
`MEMORY.md` is loaded into the startup context directly, which is why S3 passes
with the vector index broken.

The schema exposes `provider` ∈ {`openai`, `openai-compatible`, `gemini`, `voyage`,
`mistral`, `bedrock`, `deepinfra`, `github-copilot`, `lmstudio`, `ollama`, `local`}
plus `remote.baseUrl` / `remote.apiKey`. Three routes are real for us:

| Route                                                    | Credential                | Egress              | Status                                              |
| -------------------------------------------------------- | ------------------------- | ------------------- | --------------------------------------------------- |
| `openai-compatible` → an embeddings service              | none needed               | in-cluster only     | **tested, works**                                   |
| `openai-compatible` → LiteLLM + a Gemini embedding model | LiteLLM key, already held | already allowed     | untested; needs one PR                              |
| `local` + `@openclaw/llama-cpp-provider`                 | none                      | none after setup    | **blocked**, see below                              |
| `ollama` → in-cluster Ollama                             | none                      | needs a policy rule | unavailable: Ollama has a Service but **zero pods** |

**Tested and working.** A CPU `text-embeddings-inference` pod with
`BAAI/bge-small-en-v1.5` (384 dims), and `memorySearch.provider:
"openai-compatible"` pointed at it:

```text
openclaw memory index   -> Memory index updated (solo).
openclaw memory status  -> Embeddings: ready · Vector store: ready · Vector dims: 384
query "which bath toy animal represents the laboratory"
  -> 0.492 MEMORY.md:1-6  "Rai's lab mascot is a rubber duck named Ferdinand."
```

That query shares no content word with the stored line, so it is genuine semantic
retrieval rather than keyword matching. In-session the agent's `memory_search`
tool then answered "Ferdinand, the rubber duck." with **0 failures**, against the
"index needs rebuilding" error it returns with no embedding backend.

**Why `local` is blocked.** It needs `@openclaw/llama-cpp-provider`, and
`openclaw plugins install` fails on this image with an empty error while a plain
`npm install` of the same package succeeds (136 packages). Two separate causes,
one fixed and one open:

- `NPM_CONFIG_CACHE`/`NPM_CONFIG_PREFIX` pointed at `$HOME`, which is the
  read-only image layer, so every install died on `EACCES` creating `~/.cache`.
  Fixed in the manifests by moving both inside the PVC. **Any** plugin install was
  impossible before this.
- With that fixed the wrapper still fails, leaving a project dir containing only a
  117-byte `package-lock.json`. Its own `npm pack` step logs success. Not
  root-caused.

`local` remains the best endpoint on confinement grounds — no credential, no
per-query egress — so it is worth returning to via a baked image rather than a
runtime install.

**Shipped, and measured on the real path.** `gemini-embedding-2` and
`gemini-embedding-001` were added to LiteLLM (#3575) and granted to the openclaw
virtual key (#3576) — two gates, and the second was easy to miss: with the models
present but the key still scoped to the codex lane, both returned
`403 key not allowed to access model`. After both merged:

```text
POST /v1/embeddings gemini-embedding-2    -> 200, 3072 dims
POST /v1/embeddings gemini-embedding-001  -> 200, 3072 dims
```

The lab agent then moved off the temporary `text-embeddings-inference` pod and
onto LiteLLM, and the temporary pod was **deleted along with its egress rule**, so
it cannot be quietly serving the result:

```text
Provider: openai-compatible   Model: gemini-embedding-2   Vector dims: 3072
lab-embed reachable from the agent           -> blocked
"which bath toy animal represents the lab"   -> 0.444 MEMORY.md (the rubber duck)
memory_search tool in-session                -> "Ferdinand", 0 failures
```

`gemini-embedding-2` is new enough that LiteLLM's model map might not have known
it; it routes.

**Scope of the gap.** Indexing is broken for the lab instance and for
`public-coder-agent`, but **not** for the main `openclaw` gateway, which holds a
direct OpenAI Platform key and sets `memorySearch.provider: openai`. So embeddings
were unavailable to any agent _without_ that key — exactly the domain-confined
ones, whose allowlist has no route to `api.openai.com` and should not gain one
merely to embed. Routing through LiteLLM also puts embedding calls through
Langfuse, where every other model call already is.

## F14. Every config-driven pod restart breaks the session that was live

Symptom, seen twice by the user in `public-coder-agent`:

```text
⚠️ Agent failed before reply: session file changed while embedded prompt lock was
released: /home/openclaw/.openclaw/agents/coder/sessions/<uuid>.jsonl
```

Not a model or network flake. The gateway log gives the whole sequence:

```text
21:33:25  gateway starting                      (pod restarted)
21:33:25  main-session-restart-recovery: marked 1 startup-orphaned main session
21:33:30  marked interrupted main session failed: agent:coder:main
          (transcript tail is not resumable)     recovered=0 failed=1
21:34:01  next prompt starts on that same session
21:34:42  EmbeddedAttemptSessionTakeoverError
21:35:27  next run: "Merged and removed orphaned user message"  (self-heals)
```

A restart orphans the in-flight session; OpenClaw's recovery marks it
non-resumable rather than repairing it; the **next** prompt on that session trips
the takeover guard and fails. The run after that succeeds, having merged away the
orphaned turn. So it costs one prompt per restart, on the session that was live —
recurring, not random, and self-healing.

**What restarts it.** `reloader.stakater.com/auto` plus `strategy: Recreate`. The
replicaset pod templates name the trigger directly:

```text
rev 6 @21:04  last-reloaded-from CONFIGMAP public-coder-agent-config hash=5fa80f65…
rev 7 @21:33  last-reloaded-from CONFIGMAP public-coder-agent-config hash=2f6f0c9a…
```

Two content changes to that ConfigMap within thirty minutes, with no commit
touching `openclaw.json` in between. Reloader is not misbehaving — it is doing
exactly its job, and a restart is genuinely required for config to take effect,
because the `seed-config` initContainer copies `openclaw.json` onto the PVC at
startup and the gateway reads it from there.

**What changed the ConfigMap is not established.** Diagnosing it needs `configmaps`
read in `public-coder-agent`, which the debug grant does not include — it covers
pods and `pods/exec` only. Worth adding if this recurs, because "a generated
ConfigMap changes without its source changing" is the sort of thing that is
either a rendering non-determinism or another controller writing to it, and both
are worth knowing.

**Mitigations, in order of cost.** Start a fresh session after a restart, which is
what the self-heal already amounts to. Reduce restart frequency by finding the
ConfigMap churn. Neither addresses the underlying behaviour, which is upstream:
the recovery path could repair a truncated transcript instead of failing the
session, and does not.
