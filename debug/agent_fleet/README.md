# Fable-orchestrated worker fleet on cheap ChatGPT models — feasibility spike

**What this is:** an active investigation into running a fleet of cheap ChatGPT-model worker
agents (GPT-5.6 Luna) dispatched by an expensive orchestrator (Fable), all inference through the
in-cluster LiteLLM gateway. Driven from a Claude Code web session (Anthropic infra), 2026-08-28.

Sibling file: [`prior_art.md`](prior_art.md) — the external-source survey (routing tables, Codex
programmatic-drive mechanics, Claude-Code-over-gateway failure catalogue, orchestrator context
economics).

## Bottom line

**The goal — chatgpt-driven subagents — is met and verified end to end via codex, including across
compaction.** It does not require the Claude harness. Two real Luna-worker tasks were driven and
**independently checked against ground truth** (§ Real end-to-end drive):

- **Drive A** (normal window): codex/Luna wrote a module + `unittest` tests, ran them, iterated;
  _my own_ re-run of the tests passes and the implementation is correct on an independent input.
- **Drive B** (window shrunk to 24k to force compaction): on a 25k-token file, codex's stream shows
  its own multi-compaction warning, yet it produced the correct first-chunk marker
  (`QUACK-7731-ZULU`) and the correct `WIDGET` line count (517) — i.e. it ran a multi-step task
  **correctly across compaction boundaries**. This is the car actually driven around the compaction
  corner.

The `claude -p` lane is a separate, optional route and is **not** delivered:

1. **Context-window misdetection** (client-side) — root-caused; candidate fix
   `CLAUDE_CODE_MAX_CONTEXT_TOKENS=372000` only has a **precondition** checked (autocompact
   `effectiveWindow` moves `180000` → `352000`). Not a verified fix, and now moot for verification
   because the lane it runs on is down (below). Note codex's own compaction (Drive B) is a
   _different_ mechanism and its success does not transfer to the claude-p fix.
2. **The ant-messages lane is currently broken at the gateway**, independent of claude: a direct
   `curl /v1/messages` with a valid key times out (0 bytes, 35s) while `/v1/models` and
   `/v1/responses` work — so CLIProxyAPI's Anthropic-passthrough hop is unhealthy. Earlier in the
   session the same lane returned a fast `401 token_not_found_in_db` (LiteLLM logged a token
   differing from the valid key supplied) then a 60s "OAuth 401 recovery" hang. **Root cause
   unresolved** — the lane never stayed healthy long enough to run the deciding isolation
   (§ Real end-to-end drive). A flaky/down ant-messages hop is itself a strong candidate for why
   "claude -p on chatgpt felt broken."

No new haku-console build is needed to _launch_ workers — `sandbox__*` already does — but the
launched sandbox is Haku-state-flavored, not a coding-worker image.

## The breakage: `claude -p` on a gateway ChatGPT model (root-caused; fix candidate unverified end-to-end)

Reproduced live at Claude Code 2.1.251 by pointing `claude -p` at the `chatgpt/ant-messages/*`
lane. At startup, before any completion (so it is independent of auth — a placeholder key
reproduces it):

```text
"chatgpt/ant-messages/gpt-5.6-luna" is not a model this version of Claude Code recognizes, so
auto-compact will keep this session within 200k tokens (the context window it assumes). If the
model accepts more, append [1m] to the model name for 1M, or set CLAUDE_CODE_MAX_CONTEXT_TOKENS
to its real window; ... CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1 restores the
previous wait-for-the-API behavior.
[claude-code:unrecognized_model] {"model":"chatgpt/ant-messages/gpt-5.6-luna","query_source":"sdk"}
```

**Mechanism.** The gateway model slug is not in Claude Code's built-in model table, so it assumes a
200k window and runs auto-compaction against it. The real serving-path window for the gpt-5.6 Codex
models is **372k** (measured, not published — `cluster/k8s/litellm/app/model_rosters.py`
`CODEX_CONTEXT_WINDOW`; `openai_utils/probe_context_window.py` binary-searched it: 370,629 accepted
/ 372,194 rejected on 2026-07-29). So Claude Code compacts at ~166k (83% of 200k) — clipping more
than half the usable window and paying for a compaction summarization it did not need. This is
purely client-side: it does **not** depend on the CLIProxyAPI translation lane, and it hits every
custom slug (Gemini via `gemini-claude` assumes 200k of its real ~1M — wasting ~80% of the window).

**Candidate fix — only a precondition checked; NOT verified.** Setting
`CLAUDE_CODE_MAX_CONTEXT_TOKENS=372000` removes the startup misdetection warning and moves the
autocompact window input (`effectiveWindow` `180000` → `352000` in the debug log, § Live
verification). That is the lights-come-on check, not the drive: it confirms the harness _accepts_
the window, not that a real session runs smoothly across a compaction boundary on a chatgpt model.
The harmless `[claude-code:unrecognized_model]` telemetry line remains. Until a real task is driven
across compaction (still open), this stays a candidate.

| Variant                                                  | Observed                                                       |
| -------------------------------------------------------- | -------------------------------------------------------------- |
| baseline (no override)                                   | "keep within 200k" warning; `effectiveWindow=180000`           |
| `CLAUDE_CODE_MAX_CONTEXT_TOKENS=372000`                  | warning gone; **`effectiveWindow=352000`** (override applied)  |
| `CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT=1` | warning gone, reverts to old wait-for-API behavior (no window) |

Landed in this branch: `nix/home/claude_code/gateway.nix` gains optional `maxContextTokens` /
`maxOutputTokens` params (emit `CLAUDE_CODE_MAX_CONTEXT_TOKENS` / `CLAUDE_CODE_MAX_OUTPUT_TOKENS`);
`codex-claude.nix` sets 372000/128000, `gemini-claude.nix` sets 1048576/65536. The `codex-claude`
wrapper builds and renders the vars correctly. The numbers duplicate the Python SSOT in
`model_rosters.py` across the Python↔Nix boundary; a sync comment sits at both nix sites (no
integration test spans the two languages cheaply).

**Not yet done:** `tana-claude` points at real Claude model slugs (`claude-sonnet-4-6`,
`claude-haiku-4-5`) which are also unrecognized as `tana/ant-messages/*` — left alone pending
confirmation of those models' windows. A real >200k session confirming compaction fires at the new
threshold was not run (§ Real end-to-end drive exercised the window _accounting_, not a compaction
event).

## Codex as the worker harness (real completion verified)

`codex-cli 0.150.1` installed locally (`npm i @openai/codex`) and driven headlessly against the
`chatgpt/oai-responses/*` lane:

```bash
codex exec --json --skip-git-repo-check -C <dir> '<prompt>' </dev/null
```

- **Verified end-to-end with a live key** (§ Real end-to-end drive): a real completion on
  `chatgpt/oai-responses/gpt-5.6-luna` returned `agent_message: TRACE_OK` and `turn.completed`
  (`input_tokens 13083`, `output_tokens 6`).
- The structured event stream parses cleanly: `thread.started` → `item.completed` → `turn.started`
  → `turn.completed` (or, on failure, `error` retry ×5 → `turn.failed`). The modern
  `thread`/`item`/`turn` schema — a clean fleet-driver surface without touching the app-server
  JSON-RPC.
- Auth is exact: a placeholder key gets LiteLLM's
  `401 ... LiteLLM Virtual Key expected. Received=plac****-key, expected to start with 'sk-'`; a
  real `sk-...` virtual key completes.
- **Codex has the same metadata gap, but softer:** `item.completed` emits `Model metadata for
'chatgpt/oai-responses/gpt-5.6-luna' not found. Defaulting to fallback metadata; this can degrade
performance and cause issues.` It warns and proceeds rather than clamping. codex takes a valid
  `model_context_window` config key (passes `--strict-config`); **`model_max_output_tokens` is NOT a
  valid codex key** — `--strict-config` rejects it as unknown (non-strict codex silently ignores it,
  which earlier masked this). And **`model_context_window = 372000` does not silence the warning** —
  it still fired with the key set (observed 2026-08-28). So whether it changes codex's accounting is
  unverified; do not assume it "fixes" the gap. The warning is non-blocking (workers completed
  correctly). Left for a separate change surface.
- `gotcha`: `codex exec '<prompt>'` still blocks on stdin when stdin isn't a TTY (it appends a
  `<stdin>` block); always redirect `</dev/null` in a non-interactive driver, or it hangs to timeout.
- `gotcha`: `CODEX_HOME` under `/tmp` makes codex refuse to create PATH-alias helper binaries
  (warns, proceeds). Harmless for `exec`, but put `CODEX_HOME` outside `/tmp` for a real worker.

## Launching workers: what haku-console already gives us

The haku-console MCP proxy (`https://haku.allegedly.works/mcp`, reached with the
`haku-console-agent-api` bearer readable from the `haku-sandbox` namespace) already exposes an
agent-launch primitive: `sandbox__provision_sandbox` / `sandbox__exec_sandbox` /
`sandbox__dispose_sandbox` / `sandbox__list_sandboxes` (in-process server, sandbox reads
auto-approved; provision was auto-approved in this session). Provisioned `cc-fleet-probe` →
ready pod in seconds, ran bash, disposed cleanly.

**But** the provisioned sandbox is Haku's own state sandbox (`/workspace/haku-state`, user
`workspace`): **no `claude`/`codex`/`node`, no LLM env, no LiteLLM key**. Its egress is the correct
worker posture though — reaches in-cluster `litellm` (401) and `github.com` (200) but is fenced
from `api.openai.com` (502 CONNECT). So the launch/exec/dispose _primitive_ is built and usable
today; making it a _coding-worker_ launcher is config, not new code: point a sandbox template at
the `agent-workspace` image (which bakes both CLIs) and reflect a worker LiteLLM key into the
namespace. That is the "easier way that doesn't require a haku-console build-out" — reuse the
existing sandbox tool surface with a worker-flavored template.

## Real end-to-end drive (real key via hostexec, 2026-08-28)

The operator approved a `hostexec__bash` on `wyrm2` (`run_as agentydragon`) that read the
`public-coder-agent` LiteLLM virtual key (covers both `chatgpt/ant-messages/*` and
`chatgpt/oai-responses/*`).

**Drive A — a real multi-step agentic task, correct (the point of the whole exercise).** codex/Luna
(`chatgpt/oai-responses/gpt-5.6-luna`, normal window) was told to write `wordfreq.py` +
`test_wordfreq.py` (stdlib `unittest`), run the tests, and iterate to green. It ran a 4-command loop
and printed `DRIVE_A_GREEN`. **Independent check:** re-running the tests myself → 3 pass; and
`top_words("The cat, the DOG! the bird. cat dog", 3)` returns `[("the",3),("cat",2),("dog",2)]`
(correct tokenization + count-desc/alpha-asc tie-break), empty string → `[]`. So the worker produced
_correct_ code, not just self-consistent code.

**Drive B — a task driven across compaction boundaries, correct.** Same worker, but codex's
`model_context_window` shrunk to `24000` (it accepts that override) against a 25k-token file, so
codex's own auto-compaction is forced to fire mid-task. The `--json` stream carries codex's own
warning `heads up: long threads and multiple compactions can cause the model to be less
[reliable]…`, confirming multiple compactions happened. Output `result.txt` was still correct
against pre-computed ground truth: `MARKER=QUACK-7731-ZULU` (the marker is on the first line — it
survived the compactions) and `WIDGET=517` (running tally correct across them). `turn.completed`, no
failure. This is the "runs smoothly including across compaction boundaries" bar, met for the codex
worker path. (Caveat: this exercises _codex's_ compaction on the responses lane — a different
mechanism from the `claude -p` `CLAUDE_CODE_MAX_CONTEXT_TOKENS` path, which stays unverified.)

**Other observations with the key:**

- **`/v1/models` → 200, 17 models** (key valid, both lanes listed).
- **codex single-turn on `chatgpt/oai-responses/gpt-5.6-luna` → `TRACE_OK`, `turn.completed`** (real
  usage). ✅ The cheap-worker path works end-to-end. (The soft "Model metadata not found → fallback"
  warning still fires but does not block.)
- **Context-window override precondition observed** (NOT the fix — see § the breakage): `claude -p`
  on the ant-messages lane logs `autocompact: … effectiveWindow=352000` with
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS=372000`, versus `effectiveWindow=180000` on the title-generation
  subprocess that didn't inherit it. The harness _accepts_ the window; a real session compacting at
  the new threshold on this lane was never run (and now can't be, since the lane is down).
- **ant-messages lane is down at the gateway; `claude -p` never completed; 401 root cause
  unresolved.** Two moments, same lane: (1) early on, `claude -p` got a fast
  `401 {"type":"token_not_found_in_db"} … Received API Key = sk-…OwAA` while the valid key supplied
  ends `…z-0w` (confirmed non-truncated, 25 chars, url-safe) — so **LiteLLM received a token
  differing from the one provided**; claude-code then logged `OAuth 401 recovery: waiting up to
60000ms for a rotated env token` / `Failed to read OAuth token from file descriptor 4` (it treats
  the static gateway token as OAuth and hangs up to 60s on any 401 — this is the ~90s timeout). (2)
  Later, a **direct `curl /v1/messages` with the exact key (no claude) times out (0 bytes, 35s)**
  while `/v1/models` returns 200 and `/v1/responses` serves codex — so CLIProxyAPI's
  Anthropic-passthrough hop is unhealthy. A fresh-`CLAUDE_CONFIG_DIR` `claude -p` retry also hung on
  the dead upstream, so the two candidate explanations for the 401 — (a) claude-code mangling the
  token on `/v1/messages`, (b) a stale cached credential in the reused config dir — **remain
  undecided**: the lane never stayed healthy long enough to run the deciding direct-curl control.
  The lane being flaky/down on the Anthropic-passthrough hop is itself a strong candidate for the
  felt "claude -p on chatgpt is broken." Re-run the direct `/v1/messages` curl with the exact key
  when the hop is healthy to decide (a) vs (b) vs a route/key bug.

Compounding factor worth its own line: **in gateway mode, a single 401 on the ant-messages lane
costs ~60s**, because claude-code's OAuth-recovery path waits for a token rotation that never comes
for a static key. Any transient auth hiccup on that lane becomes a minute-plus hang, not a clean
error — a real usability cliff for an unattended worker.

## What blocked a live completion before the hostexec read

A live `sk-...` key is needed; from a web session three no-approval routes were closed (the
operator-approved hostexec read above got past this):

- **SOPS decrypt** — dead this session: the session-start hook's `claude-web` age identity did not
  match recipients (`github-pat`, aiquota bearer, and the pinned `litellm-*-clients-key.yaml`
  files all fail to decrypt here). Not just a recipient-list gap — the private key isn't
  functioning in this session.
- **kubectl read of `litellm` secrets** — the web session's own identity can't
  (`get secrets -n litellm` = no), and haku-console's operator-bound kubectl passthrough is
  **also denied** (`grants__kubernetes_can_i` → `allowed:false` for `litellm-key-public-coder-agent`
  and `litellm-master-key`). So approving a `kubectl_passthrough` read would still fail RBAC — no
  point sending it.
- **sandbox baked key** — none, as above.

The web session _can_ read secrets in `haku-sandbox`. So the clean durable mechanism (and the one
that unblocks future sessions with zero approval dance) is to **mint a worker key and reflect it
into `haku-sandbox`** (ESO/reflector, the same pattern the aiquota bearer uses into
`haku-egress-proxy`), then read it with kubectl. Alternatively add `claude-web` as a real SOPS
recipient once its age key works, or the operator hands over a scoped key directly.

## Economics (why this is worth doing)

The lever is the orchestrator/worker token split, not per-token price. `prior_art.md` §4: an
orchestrator's own file-reading + planning routinely dwarfs the workers' combined output (one
like-for-like anecdote: ~9M orchestrator tokens vs ~1.2M worker tokens for ~3.5× the worker's code
output). Moving the read/implement grind to Luna (index ~47 at high effort, ~2¢/task-equivalent —
`docs/ai_subscription_comparison.md`) while Fable pays only for specs/review/merge is where the
~$10k splurge compresses. The OpenAI subscription lane is flat-rate and its 5h window rarely binds
(operator), so marginal worker cost ≈ 0 until the weekly window — making the delegation floor
(`prior_art.md` §4.4: a worker pays off past ~500k tokens of reading or 3+ warm-cache reuses)
the real dispatch heuristic, not raw price.

**Load-bearing caveat** (`prior_art.md` §4.2, Cognition's production writeup): _cheap workers
paired with a Claude orchestrator work; cheap workers paired with a cheap orchestrator don't yet_
— a judgment/training gap, not a prompting one. So keep Fable (or an equivalent frontier tier) as
the orchestrator; don't let a Luna worker sub-dispatch to more Luna. This matches the archived
doctrine (`haku/archive/2026_08_multi_agent.md`): dispatch only to strictly-subset-privileged,
spend-capped principals.

## Next steps (not done here)

1. **Durable worker key**: mint `claude-web-workers` (or similar) in `tf/gitops/litellm-keys`
   (both `chatgpt/ant-messages/*` and `chatgpt/oai-responses/*`, Luna fallback, a real budget cap
   — unlike the uncapped `agent_workspaces_codex`), reflect into `haku-sandbox`, add a
   `try_export_from_k8s` in `devinfra/secrets/web_env.sh`. Then a web session dispatches workers
   with no approval dance.
2. **Live-fire verification**: with that key, confirm a real Luna completion on both lanes and that
   the 372k window fix yields a healthy >200k session.
3. **Codex config metadata**: add `model_context_window`/`model_max_output_tokens` to the baked
   codex configs.
4. **Worker-flavored sandbox template**: point a `sandbox__provision` template at the
   `agent-workspace` image + reflected key, turning the existing launch primitive into a
   coding-worker launcher.
5. **aiquota**: not inspected (operator says the 5h window rarely binds; would need a bearer read).
   Reachable via haku-console if a live quota snapshot is wanted.

## Reproduction notes

- MCP to haku-console: `POST https://haku.allegedly.works/mcp`, `Authorization: Bearer <token>`
  where the token is `kubectl -n haku-sandbox get secret haku-console-agent-api -o
jsonpath={.data.token} | base64 -d`. Server is **stateless** Streamable HTTP — do **not** send an
  `mcp-session-id` header (an empty one makes calls return 0 bytes). Responses are SSE; the JSON is
  the `data:` line.
- Claude Code window probe: the warning is on stderr at startup and appears with a placeholder key,
  so capture full output to a file (`… >out 2>&1`) rather than piping through `grep` under
  `timeout` (the kill drops buffered output).
