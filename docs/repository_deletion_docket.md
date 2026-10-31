# Repository cleanup campaign ledger

> **Status:** historical draft scratchpad, not a merge deliverable and not an
> implementation authorization.

This document records the August 2026 repository-cleanup campaign and the small
set of decisions that remained after its completed deletions. It was refreshed
against
[`ac24ed8f7`](https://github.com/agentydragon/ducktape/commit/ac24ed8f71f1bb3c9c6ab90f81c0b2602c34486a)
on 2026-08-26.

The original audit ranked 15 candidates by recurring programmer cost rather
than repository bytes. Most of its strongest recommendations have since merged.
Detailed implementation findings belong in the merged PRs and Git history; this
ledger intentionally does not preserve those obsolete recommendations as a
second permanent TODO system.

Current tracking ownership remains:

- root [`TODO.md`](../TODO.md) for repository-wide actionable work;
- package-local TODO files for package-specific findings;
- this draft only for cleanup-campaign history and unresolved deletion choices.

Approval of one remaining item does not approve adjacent items. Any
implementation must still start with a current reference, history, dependency,
manual-entry-point, and compatibility audit.

## Completed campaign items

| Original ID | Outcome                                                                                                                                                                                                                               | Whole-tree result |
| ----------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------: |
| C04         | [#4660](https://github.com/agentydragon/ducktape/pull/4660) deleted the retired `x/claude_linter_v2` implementation while preserving the active Rust Claude hook.                                                                     |    **−6,915 LOC** |
| C05         | [#4661](https://github.com/agentydragon/ducktape/pull/4661) deleted the obsolete CI release-decision stage while preserving native release triggers, content-addressed artifacts, and independent release/security oracles.           |      **−801 LOC** |
| C06         | [#4693](https://github.com/agentydragon/ducktape/pull/4693) deleted the deliberately broken Props GEPA integration, regenerated dependency metadata, and retained a concise revival decision in `props/TODO.md`.                      |    **−1,316 LOC** |
| C07         | [#4666](https://github.com/agentydragon/ducktape/pull/4666) deleted `x/editor_agent` separately from `x/agent_server`, preserving shared `agent_core` and MCP infrastructure.                                                         |    **−1,287 LOC** |
| C09         | [#4749](https://github.com/agentydragon/ducktape/pull/4749) replaced generated-policy roster mirrors with automated final-artifact integration checks for Claude, Codex, and Gemini.                                                  |      **−221 LOC** |
| C10         | [#4697](https://github.com/agentydragon/ducktape/pull/4697) intentionally removed seven deprecated Debundle CLI spellings while preserving canonical commands, public report APIs, internal `peel` terminology, and frozen specimens. |      **−205 LOC** |
| H02         | [#4744](https://github.com/agentydragon/ducktape/pull/4744) deleted the unwired `//cluster/validation:kustomize_build_all` wrapper while retaining the cluster integration validator.                                                 |       **−80 LOC** |

These items are absent from current `devel`; do not re-audit or re-propose them
from the old candidate text.

## Remaining subsystem decisions

The ordering below reflects current implementation risk, not prior acceptance
percentages. None is approved merely by appearing here.

| Order | Candidate                               |                                            Current size | Decision state            | Principal precondition                                                                           |
| ----: | --------------------------------------- | ------------------------------------------------------: | ------------------------- | ------------------------------------------------------------------------------------------------ |
|     1 | [`x/eob_matching/`](../x/eob_matching/) |                                  11 files / 1,556 lines | unapproved                | confirm the one-off PDF extraction workflow is no longer useful                                  |
|     2 | [`x/inop/`](../x/inop/)                 |                                  42 files / 7,587 lines | unapproved                | confirm no undocumented/manual use of its direct optimizer entry point                           |
|     3 | [`x/agent_server/`](../x/agent_server/) |                                165 files / 17,771 lines | unapproved broad campaign | define preservation owners for reusable lifecycle/cancellation research and planning conclusions |
|     4 | Squid egress spike                      | about 2,500–2,800 lines plus registered GitOps workload | **blocked**               | resolve #4043 protocol provenance and explicitly decide #2798                                    |

### `x/eob_matching`

The subtree is self-contained: current repository searches find no external code,
BUILD, workflow, or documentation consumer. Its README says the motivating
question was answered through PDF extraction without completing the matcher.

Before deletion, confirm that extraction is not a retained personal utility,
inspect history and manual entry points, and verify all dependencies are
subtree-local. If confirmed, delete the complete experiment rather than repair a
matcher whose recorded task is already finished.

### `x/inop`

No registered package, binary, CI job, or repository caller was found. The only
current external references are inventory entries in
[`docs/flat_tool_convertible.md`](flat_tool_convertible.md) and
[`docs/gazelle_python_status.md`](gazelle_python_status.md). However,
`x/inop/engine/optimizer.py` has a direct `main()` path, so absence from Bazel or
CI is not proof that nobody invokes it manually.

A deletion audit must check personal/manual use, history, developer scripts,
Docker tests, BUILD wiring, and whole-tree dependency consumers. If accepted,
delete the entire subsystem, update both documents and Ruff/build configuration,
and remove dependencies only through the owning requirements and Gazelle
generators.

### `x/agent_server`

This has the largest payoff but is not a quick cleanup. It includes Python and
Svelte applications, persistence, container runtime, approval policy, MCP,
Matrix, E2E tests, pnpm/ESLint wiring, and research documents. Current planning
already rules it out as a reuse base, but several documents still depend on its
architectural findings.

Any retirement plan must:

- preserve shared [`agent_core/`](../agent_core/) and its live consumers;
- preserve or rewrite conclusions in
  [`plans/oauth_architecture.md`](../plans/oauth_architecture.md) and
  [`plans/personal_agents/survey/cross_cutting.md`](../plans/personal_agents/survey/cross_cutting.md);
- move only genuinely reusable MCP lifecycle/cancellation research to a
  canonical owner;
- remove pnpm, MODULE, ESLint, pre-commit, dependency, BUILD, and documentation
  wiring in the same campaign;
- leave frozen `props/specimens/**` inputs unchanged.

### Squid spike

The spike and stale migration plan remain blocked while open PR
[#4043](https://github.com/agentydragon/ducktape/pull/4043) uses their measured
ICAP behavior as protocol provenance. Old draft
[#2798](https://github.com/agentydragon/ducktape/pull/2798) also still presents
the abandoned Squid migration.

Do not retire the spike until #4043 has a canonical provenance owner and #2798
has an explicit status decision. A later GitOps removal must delete the Flux
registrations first, verify pruning, and explicitly delete the namespace because
its namespace Kustomization does not prune it automatically.

## Lower-priority archival candidates

These are discovery results, not an actionable backlog. Re-check references and
migrate any surviving conclusion before deleting them. Prefer one coherent
archive-pruning PR only when the files share the same historical-ownership
reason.

| Candidate                                                                                                         |           Current size | Current inbound-reference finding                                                              |
| ----------------------------------------------------------------------------------------------------------------- | ---------------------: | ---------------------------------------------------------------------------------------------- |
| [`cluster/archive/2026_04_architecture_redesign/`](../cluster/archive/2026_04_architecture_redesign/)             |  4 files / 1,005 lines | none found outside the archive                                                                 |
| [`cluster/docs/archive/2026_02_bootstrap_analysis.md`](../cluster/docs/archive/2026_02_bootstrap_analysis.md)     |              331 lines | none found                                                                                     |
| [`cluster/archive/2026_05_sre_best_practices_review.md`](../cluster/archive/2026_05_sre_best_practices_review.md) |              448 lines | none found                                                                                     |
| [`haku/archive/2026_08_instructions_ownership.md`](../haku/archive/2026_08_instructions_ownership.md)             |              116 lines | none found; prefer compression if unique unresolved ownership remains                          |
| [`mcp_infra/docs/mcp_tool_name_violations.md`](../mcp_infra/docs/mcp_tool_name_violations.md)                     |              271 lines | none found                                                                                     |
| [`cluster/archive/2026_05_raw_pvc_inventory.md`](../cluster/archive/2026_05_raw_pvc_inventory.md)                 |               45 lines | none found                                                                                     |
| [`archive/2026_04_sops_nix_container_activation.md`](../archive/2026_04_sops_nix_container_activation.md)         |              316 lines | none found                                                                                     |
| [`x/claude_commands_old/`](../x/claude_commands_old/)                                                             |    6 files / 919 lines | explicitly archived and low recurring cost                                                     |
| [`cluster/archive/2026_07_kagent/`](../cluster/archive/2026_07_kagent/)                                           | 35 files / 2,063 lines | referenced by four current survey/planning documents; preserve explanatory retirement evidence |

For Kagent, deleting retired IaC snapshots may be reasonable, but current links
to the README and persistent-agent analysis must survive through retained prose
or deliberate historical links. Do not blindly delete the whole archive.

## Explicitly parked or preserved

Do not turn these into cleanup PRs from this ledger without changed
preconditions:

- OpenClaw/Haku Codex runtime work remains a separate track. PR #4584 has
  merged; do not absorb its rollout or follow-up compatibility work here.
- Haku documentation cleanup merged separately in PR #4683; do not duplicate
  it from this ledger.
- LiteLLM exact-config test work is already owned by PR #4472.
- Raw Inspect-AI `.eval` archives remain explicitly deferred.
- `x/claude_commands_old/` is optional housekeeping, not a roadmap driver.
- Props specimens are frozen inputs unless their own pruning rules authorize a
  reference-audited deletion.
- Migrations, schema compatibility tests, negative authorization tests, exact
  approval/audit behavior, release/security oracles, postmortems, and unique
  operational lessons remain protected.
- Rejected Haku manifest and Augur reducer prototypes removed no complete stage
  or weakened an independent oracle; do not revive them as cleanup work.

## Implementation standard

For every separately authorized deletion:

1. create or reuse a dedicated worktree and rebase onto current `origin/devel`;
2. repeat inbound-reference, BUILD/package, history, dependency, and manual-use
   audits;
3. preserve user-visible behavior and independent semantic, security,
   compatibility, deployment, release, and integration oracles;
4. remove generated dependency entries through their owning generators;
5. report raw additions/deletions and the whole-tree net result;
6. run changed-file pre-commit plus the owning unit/integration/E2E checks;
7. obtain independent semantic review before opening or marking a PR ready;
8. keep the PR focused and independently reversible.

This draft should remain open only as a campaign history and decision aid. It
must not be marked ready or merged, and it must not become a duplicate of root
or package-local TODO files.
