# Documentation hygiene audit — outstanding items (2026-07-08)

Full repo-wide documentation/comment hygiene audit (README/AGENTS/SPEC/PLAN/TODO
files, debug notes, and code comments/docstrings across both `ducktape` and
`gaffer-private`). A first batch of the highest-confidence findings was fixed
and merged via [ducktape#2998](https://github.com/agentydragon/ducktape/pull/2998)
and [gaffer-private#375](https://github.com/agentydragon/gaffer-private/pull/375).

This note tracks everything else the audit found but that wasn't part of that
batch — either lower priority, needing a human judgment call, or not yet
triaged. IDs are stable references from the original audit conversation.
Delete an item's row once it's fixed (this is a live tracking list, not a
historical record).

## P1 — high confidence, high consequence (not yet fixed)

- **I** — `devinfra/bazelization/STATUS.md`: 5-months-stale migration doc; points at a
  nonexistent `//tools:gazelle` target (real: `//devinfra:gazelle`), cites a phantom
  `bazel lint //...` command, lists an already-completed TODO. Rewrite or delete.
- **U** — `props/core/gepa/README.md`: documents a working `props gepa --max-metric-calls
  100` CLI; the function it calls unconditionally raises `NotImplementedError`, and the
  doc cites a database `events` table that no longer exists. Mark broken or delete
  pending the described migration.
- **W** — `desk/wiring_schematic.dot`: the atlas node only has a port anchor for `tb_top`
  (no anchor for the middle port), and the `atlas:tb_top -> kvm:pc1` edge sends the
  Sabrent tag-4 cable to the top port. `desk/README.md` and `desk/debug/build_log.md`
  both confirm the cable is actually on the **middle** port (top only carries BIOS
  video, not kernel DP-Alt — confirmed by the 2026-07-01/07-02 build-log entries).
  Needs a `<tb_middle>` port anchor added to the `atlas` node and the edge repointed;
  regenerate the SVG (`dot -Tsvg desk/wiring_schematic.dot -o desk/wiring_schematic.svg`)
  per `desk/AGENTS.md`. Deferred pending explicit go-ahead (discussed in chat).
- **X** — `nix/docs/private_flake_inputs.md`: "Current status" says the
  gaffer-private/google-drive module is commented out everywhere; it's actually live in
  `flake.nix` and enabled on rugged and wyrm2. Rewrite around the current cache-pin
  architecture.

## P2 — verified, moderate impact

- **AB** — `README.md` convention text claims `cluster/` uses `lessons_learned/` "instead
  of" `debug/`; both are actually used (raw vs. distilled).
- **AF** — `cluster/TODO-wyrm2-tofu-sync.md`: self-describes "delete after applying," 3+
  months stale. Verify whether the tofu apply happened; delete or fold into
  `k8s/TODO.md`.
- **AG** — `cluster/docs/plans/offline_node_daemonset_health.md`: lists a retired node
  (`talos-pve-cp-0`) as offline-prone.
- **AJ** — `devinfra/buildbuddy_cli/docs/python-grpc-stubs.md`: frames an already-solved
  problem (the `bbapi` CLI exists) as open.
- **AK** — `devinfra/claude/web_setup.sh:47-55`: tombstone's gate ("≥1 week of live
  sessions") passed 3 months ago.
- **AL** — `devinfra/secrets/ci_env.sh`: two tombstoned secrets files
  (`attic-token.sops.yaml`, `ghcr-credentials.sops.yaml`), zero in-tree consumers,
  unverifiable "once safe" removal gates.
- **AM** — `haku/base/{instructions,README,AGENTS}.md`: 3 copies of the agent
  data-source list all omit `cpap`/`grocy`/`activitywatch`/`mailbox`.
- **AN** — `haku/plans/managed_agents_artifacts.md`: contradicts a sibling doc on SDK
  choice (says "ant-all-the-way," actual worker uses the Anthropic Python SDK).
- **AO** — `haku/runtime/managed_agent/self_hosted/haku.agent.yaml`: model pin has a
  `TEMP` marker (should be `claude-opus-4-8`) with no tracked revert.
- **AP** — `skills/freecad/BUILD.bazel`: dead `oci_layout_rloc` target per the
  tombstone's own stated condition.
- **AR** — `x/agent_server/docs/subagent_capability_tokens.md`: stale duplicate of a doc
  already marked OUTDATED in `props/docs/plans/`.
- **AS** — `x/agent_server/docs/matrix.md`: documents a different API than
  `mcp/matrix_server.py` actually ships.
- **AT** — `x/mcp_oauth_facade/`: production-critical (6+ live cluster deployments)
  still labeled experimental under `x/`.
- **AU** — `x/study_casino/`: live production service (real URL, HA DB, Flux image
  automation, Terraform SSO) still under `x/`.
- **AV** — `x/codex_pod_image/`: CI-built, Flux-deployed, RBAC-gated image still under
  `x/`.
- **AW** — `x/sandboxed_jupyter/docs/{CLI_DESIGN.md,CONFIG_DETAILS.md}`: two competing
  CLI designs, neither matches the real `wrapper.py` argparse.
- **AX** — `x/ember_evals/{runner.py,definitions.py}`: non-importable — imports 5
  nonexistent modules, no `BUILD.bazel`.
- **AY** — `x/gitea_mirror/gitea_mirrors_setup.md`: entire runbook describes a replaced
  (pre-Flux, helmfile-based) architecture.
- **AZ** — `x/rspcache/README.md`: dead `adgn/`-era paths and a nonexistent Helm chart.
- **BA** — `x/linux_rac/authentik_rac_setup.md`: describes Helm/RAC automation that
  appears never actually built.
- **BB** — `x/linux_rac/linux_desktop_provisioning.md`: references an Ansible
  role/inventory group that doesn't exist.
- **BC** — `x/inop/data/graders.yaml`: full duplicate of the wired-up
  `graders_consolidated.yaml`.
- **BD** — `x/inop/docker/build.py`: builds Dockerfiles that have never existed in this
  tree.
- **BE** — `x/benchmark_ollama/*`: every command targets a nonexistent Bazel package
  (real: `//x/benchmark_ollama`).
- **BF** — `x/benchmark_ollama/benchmarks.md` vs `report.md`: predicts a fix that a
  later doc already found regresses.
- **BG** — `x/web_desktop_requirements.md`: "open questions" already resolved by
  `x/linux_rac/`.
- **BH** — `props/docs/plans/agent_core_to_agent_framework.md`: marked "not scheduled";
  already shipped.
- **BI** — `props/docs/{agent_infrastructure,agent_loop_inside_container}.md`: describe
  the old pre-migration architecture; also still cite the now-fixed `list_pending`
  tool name (see `props/agents/grader` fix in #2998) and a nonexistent
  `props/agents/grader/loop.py`.
- **BJ** — `props/docs/SPEC.md`: cites a dead module; documents the wrong REST endpoint
  for listing definitions.
- **BL** — `tana/export/SEARCH_README.md`: pre-reorg paths; usage examples fail outside
  Bazel.
- **BM** — `tana/litellm_proxy/TODO.md`: P0 items already implemented and deployed.
- **BN** — `finance/plaid/link/README.md`: stale title/targets predating a package
  rename.
- **BO** — `finance/README.md`: "Components" omits the entire `plaid/` subtree.
- **BP** — `mcp_infra/docs/{mcp_tool_name_violations,mcp_sanitization}.md`: cite
  classes/paths that no longer exist.
- **BQ** — `gmail_archiver/README.md`: wrong CLI example, stale table, missing
  subcommands, stale scope framing.
- **BR** — `website/README.md`: "Upgrades" describes a Stack toolchain; build is
  Bazel.
- **BS** — `gnome/README.md` + root index: documents a component that moved to
  `aiquota/gnome/`.
- **BT** — `idea/hetzner_auction_k8s_node.md`: already rejected elsewhere (the
  filename-reference part was fixed in #2998; the tombstone/status framing is still
  open).
- **BU** — `idea/README.md`: index lists 1 of 4 real idea docs.
- **BV** — `ansible/atlas.yaml:101`: comment contradicts the doc it cites (NVENC
  claim).
- **BW** — `ansible/README.md`: missing deploy instructions for `atlas.yaml`.
- **BX** — `aiquota/gnome/NOTES.md`: wrong Codex endpoint/auth mechanism.
- **BY** — `llm/html/README.md`: references a server file that doesn't exist.
- **BZ** — `nix/README.md`: host lists omit `agent-box`.
- **CA** — `homeassistant/notes.md`: stale household table; self-contradicting SSH
  claim.

## P3 — low (condensed)

- **CH** — 27 files with identical boilerplate RBAC annotation under
  `cluster/k8s/*/agent-rbac/` — trim to specifics.
- **CI** — `cluster/docs/plan.md`: same TODO item duplicated twice in the file.
- **CK, CL, CM, CN** — `cluster/`: 3 files break the `lessons_learned/` naming
  convention; an unresolved "open" status 7wks stale; a plan mislabeled as WIP; a
  decommissioned plan still says "Draft/In Progress."
- **CQ** — `devinfra/claude/docs/{hook_call_patterns,hook_semantics_audit}.md`:
  investigation notes filed in `docs/` instead of `debug/`.
- **CR, CS, CT, CU, CV** — `haku/skills`: dead PLAN.md anchor; PLAN/TODO duplication;
  pitfalls repeated 2-3x across files; 3 broken path references; stale status +
  duplicated followups.
- **CW, CX, CY, CZ, DA** — `x/agent_server`, `gitea`, `sandboxed_jupyter`, misc `x/`:
  already-fixed bug presented as open; dead pre-restructure paths; duplicate TODO
  lists; broken workflow/target references; stale feature claims.
- **DB, DC, DD, DE** — `x/sysrw`: dead pre-rename command references; duplicated dict
  instead of import; nonstandard template syntax; stale snapshots outside `archive/`
  + a glob bug.
- **DF** — `x/inop`: dead `DEBUGGING.md` references; 17-line changelog comment.
- **DG, DH, DI, DJ, DK** — `x/` misc: wrong path; 5.5-month-stale progress table;
  undocumented env var/healthcheck; feature-matrix contradiction; duplicated SSH
  bootstrap steps; broken import in an orphaned benchmark.
- **DL, DN, DO, DP** — `props/`: renamed-file references; duplicated examples across
  standards + prompts; 9 orphaned docs never wired in; an already-done TODO item.
- **DQ, DR, DS, DT, DU** — `tana/finance/loom`: abandoned ideation doc; duplicated TODO
  items; already-true rename precondition; stale top-of-file status; ~650 lines of
  generic background bloat in `finance/augur/docs/prior_art_audit.md`.
- **DV–EK** — misc (`mcp_infra`, `found`, `website`, `nix`, `llm`, `claude_commands`,
  `openai_utils`, `ansible`, `tf`): stale/incomplete component lists, orphaned
  research docs, wrong doc references, duplicated module lists and deploy commands,
  stale lint excludes, an ambiguous "root package.json" reference.

## P4 — optional/cosmetic

Malformed markdown links; understaffed-progress TODOs needing owner confirmation;
unlanded skill-improvement proposals; a missing "use when" trigger clause; clusters
of pure changelog comments and docstrings that just restate a function's own
name/signature (`x/claude_history`, `x/domains`, `x/inop`, `gnome/gterm_theme`,
`tana/query/search`, `props/testing/mocks.py`); an unmarked design-only spec
(`x/fancy_terminal/ai_suggest_daemon_spec.md`); duplicate back-to-back comments in
`x/webhook_inbox/webhook_inbox.py`; an off-by-one self-referential count in
`props/testing/AGENTS.md`; a stray non-`.md` scratch file (`props/core/pytest-todo`);
a stale parenthetical host name in the root README; small ambiguous references
(`third_party/manifold_mcp_server`, `ansible/tasker`).

## Not covered by this audit

The `wt/`, `grocy_mcp/`, and `airlock/` subtrees were assigned to a sub-audit that
never returned a result — genuinely unaudited, not "found clean."
