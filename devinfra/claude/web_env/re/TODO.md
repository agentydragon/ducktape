# RE gaps — environment-manager `0b86a2a0`, process_api `edebff2c`

Cross-cutting items that survived the 2026-07-31 pass. Gaps local to one
function are marked `TODO(re):` in the source instead (142 of them); this file
is for the ones worth planning around.

Read <environment_manager/README.md> § Working with the binary first — the
toolchain notes there (symbol recovery, literal decryption, the degarble map)
are what these items depend on.

## Blocked on decrypting function-local literals

Everything here is blocked by the same thing: garble `-literals` leaves these
strings encrypted until the containing function runs, and the containing
function is the feature under investigation. The `reverse_engineer` skill
documents a forced-execution probe that decrypts a local without running its
feature; where that failed, it failed because several literals share one
decryption state machine and the individual block's entry point could not be
isolated.

- **`failed_stage` / `failure_category` value sets.** Produced by the session
  runner and marshalled into the work result. The candidate stage set is a
  slice on the runner; the strings are rebuilt by an inline XOR/ADD/SUB ladder
  keyed off a runtime global.
- **A fifth `claim_miss` reason.** Seven are enumerated (see
  `internal/spare/`); an eighth emit site exists whose 38-byte buffer was not
  decrypted.
- **Codesign MCP tool names and schemas.** `GetTools` never runs under
  `--help`, so its literals are in neither the binary nor a core. The tool list
  currently in the tree is CARRIED from the a6f96673 DWARF era and unverified.
- **The MCP config file's JSON shape** and how `mcpConfigPath` is resolved.
- **OTLP trace endpoint URLs.** The backend supplies them; the values are
  decrypted inline from a runtime global. The tuple shape is known —
  `{logs, metrics, traces, headers}` — but not the field order.

## Blocked on reading a consumer

- **`git_via_egress_gateway`**, **`baku_backend_provider`**,
  **`sparse_checkout_paths`** — all three are confirmed present in the task-run
  input with exact types and offsets, but no reader was located for any of
  them, so what they _do_ is unknown. Do not infer behaviour from the field
  names; `CCR_EGRESS_GATEWAY_ENABLED` exists in both binaries and so is **not**
  evidence for the first one.
- **`LauncherHook.event` vocabulary** — `GetHooks` not disassembled.
- **The claim payload beyond `session_id`**, and whether `Claim` reads a reply.
- **`_FILE_DESCRIPTOR`** — the spare's `Claim` builds an env map and compares
  against this 16-char key, which looks like an fd-passing hook. The full
  variable name lives in the caller.

## Not recoverable from this binary

- **Startup-timing phase names.** Both `phases` and `phase_start_ms` are
  decoded generically and there is no literal-keyed lookup into either map
  anywhere in `.text`. The vocabulary belongs to the Claude Code CLI that
  _produces_ the timings, so enumerate it from the CLI bundle or from a
  captured `system`/`init` stream line — not from environment-manager.
- **Per-file source layout.** Garble randomizes filenames per function; see
  <environment_manager/degarble_map.md>.

## Carried forward, not re-verified

- **envtype `Initialize` session-mode gating.** The `new` / `resume` /
  `resume-cached` / `setup-only` step gating in <../../AGENTS.md> was
  established on an older binary. `Initialize` grew from 103 to 157 symbols and
  gained seven timing wrappers, but the mode dispatch itself looks unchanged —
  in both binaries exactly one function references the `resume-cached` literal.
  Re-read the gating before relying on the step-by-step detail.
- **`RecordLongRunningStep`** is documented on the o11y service but no such
  symbol exists in this build. Stale, not deleted, pending a check of whether
  it moved or went away.
- **`process_api` `src/trace.rs`** has never been recovered in any version —
  panic locations exist, the module does not. Not a delta; a standing gap.

## Environment-limited

- **Container build-and-diff (Phase 5) has not been run** against this
  snapshot. The session that produced it had the `docker` client but no daemon.
  `live-dpkg-versions.txt` is refreshed (same 686 packages, version bumps
  only), but `tools/fetch_debs.py` `SNAPSHOT_DATES` was deliberately left
  alone: a single new date is provably insufficient, because the live `curl`
  (`8.5.0-2ubuntu10.8`) is _older_ than the 2026-07-31 archive snapshot
  (`10.11`), so the list needs an intermediate entry. Advancing it requires a
  machine that can verify the fetch and the resulting image diff end to end.

## Update 2026-07-31 (later pass): inline immediates

A third literal-recovery channel turned up, independent of the core dump and of
any live process. **garble `-literals` cannot hide a short string that is
compared inline**, because the constant is an instruction operand, not data. Go
emits such a comparison as a length check plus compares at increasing
displacements off the string pointer:

```text
cmpq    $0xd, 0x28(%rcx)              ; len(s) == 13
movabsq $0x632d656d75736572, %r12     ; "resume-c"
cmpq    %r12, (%rdx)
cmpl    $0x65686361, 0x8(%rdx)        ; "ache"
cmpb    $0x64, 0xc(%rdx)              ; 'd'      -> "resume-cached"
```

Sweeping `.text` for ASCII immediates recovered 333 strings. Confirmed from the
app packages: `resume`, `resume-cached`, `setup-only`; `github`, `git+ssh`,
`ssh+git`, `fresh`; `network`, `timeout`, `unknown`, `not_found`, `rate_limit`,
`server_error`, `clone_timeout`, `ref_not_found`; `latest`, `stable`,
`current`; `system`, `result`; `golang`, `nodejs`, `python`.

**This resolves the "carried forward" item on envtype gating.** The session-mode
dispatch is _not_ confined to the runner: `CWddODOS8sH.(*i9eIWNpWL).Initialize`
compares against `resume-cached` at three sites (`0x238ddb1`, `0x238de05`,
`0x2392105`), so mode gating does live inside `Initialize` on this binary, as
<../../AGENTS.md> describes. The step-by-step gating still wants reading, but
the structural premise is confirmed rather than assumed.

The `not_found` / `rate_limit` / `server_error` / `clone_timeout` /
`ref_not_found` set sits in `WWD9Ee6Wrf4m.Bd3trAjD.GetUserMessage` — a backend
error taxonomy. Whether it is the same vocabulary as `failure_category` is
**not** established; do not assume it.

Caveat on the sweep: it reliably recovers the first fragment of each comparison,
so strings of 8 bytes or fewer come out whole and longer ones come out
truncated (`resume-c` for `resume-cached`) unless the fragments are reassembled
by displacement, which has to be done per-site by hand for now.
