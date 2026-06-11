# Specimens Pruning Backlog

**Current total: ~76 MB across 11 ducktape snapshots.**

## Remaining unreferenced top-level dirs in 2026-01-17-00 (~3.5 MB, low risk)

| Dir                  | Size  | Notes                                                 |
| -------------------- | ----- | ----------------------------------------------------- |
| `adgn/`              | 280K  | Legacy package remnant, no issues                     |
| `inventree_utils/`   | 220K  | Inventory plugins, no issues                          |
| `sandboxed_jupyter/` | 204K  | Jupyter sandbox, no issues                            |
| `rspcache/`          | 168K  | Cache util, no issues                                 |
| `difftree/`          | 156K  | Diff tool, no issues                                  |
| `terraform/`         | 136K  | IaC, no issues                                        |
| `homeassistant/`     | 128K  | HA config, no issues                                  |
| `trilium/`           | 120K  | Notes extensions, no issues                           |
| `dotfiles/`          | 96K   | Shell configs, no issues                              |
| `editor_agent/`      | 88K   | Editor agent, no issues                               |
| `docs/`              | ~60K  | Remaining prompts/docs, no issues                     |
| 13 smaller dirs      | ~300K | `bazelization/`, `third_party/`, `arg0_runner/`, etc. |

## Remaining unreferenced top-level dirs in 2026-01-29-00 (~4.6 MB, low risk)

| Dir              | Size  | Notes                                                        |
| ---------------- | ----- | ------------------------------------------------------------ |
| `claude/`        | 664K  | Claude integration, no issues                                |
| `prompts/`       | 580K  | Prompt templates, no issues                                  |
| `sysrw/`         | 416K  | System read/write util, no issues                            |
| `inop/`          | 312K  | Inop tool, no issues                                         |
| `agent_core/`    | 228K  | Agent core, no issues                                        |
| `rspcache/`      | 168K  | Cache util, no issues                                        |
| `openai_utils/`  | 160K  | OpenAI utils, no issues                                      |
| `difftree/`      | 156K  | Diff tool, no issues                                         |
| `git_commit_ai/` | 156K  | Commit AI, no issues                                         |
| `homeassistant/` | 128K  | HA config, no issues                                         |
| `trilium/`       | 120K  | Notes extensions, no issues                                  |
| `editor_agent/`  | 84K   | Editor agent, no issues                                      |
| `agent_pkg/`     | 80K   | Agent packaging, no issues                                   |
| `docs/`          | ~68K  | Remaining prompts/docs, no issues                            |
| 14 smaller dirs  | ~400K | `agent_core_testing/`, `third_party/`, `bazelization/`, etc. |

## Other unreferenced dirs in older snapshots (~2.5 MB, low risk)

| Dir                                                      | Savings | Snapshots   |
| -------------------------------------------------------- | ------- | ----------- |
| `adgn/rspcache_admin_ui/` (incl 80K `package-lock.json`) | ~1.1 MB | 8 snapshots |
| `adgn/examples/`                                         | ~0.5 MB | various     |
| `adgn/docker/`                                           | ~0.4 MB | various     |
| `adgn/gitea_pr_gate/`                                    | ~0.4 MB | various     |
| `adgn/instructions/`                                     | ~0.2 MB | various     |

## Unreferenced `adgn/src/adgn/` subdirs (~10 MB, medium risk)

Across 8 `adgn/`-era snapshots, most subdirs of `adgn/src/adgn/` have no issue references.
Example from `2025-11-20-00` (only 3 of 12 subdirs referenced):

| Subdir           | Size | Referenced? |
| ---------------- | ---- | ----------- |
| `props/`         | 640K | No          |
| `mcp/`           | 572K | No          |
| `inop/`          | 288K | No          |
| `openai_utils/`  | 120K | No          |
| `rspcache/`      | 84K  | No          |
| `git_commit_ai/` | 60K  | No          |
| `seatbelt/`      | 40K  | No          |
| `tools/`         | 28K  | No          |
| `util/`          | 16K  | No          |

**Caution:** These may be indirect dependencies (imported by files that ARE referenced).
Needs per-snapshot import tracing before deleting. Each snapshot has ~1.5-2 MB prunable
if imports are verified clean.

## Summary

| Category                                    | Est. Savings | Risk   |
| ------------------------------------------- | ------------ | ------ |
| Unreferenced top-level dirs (2026-01-17-00) | ~3.5 MB      | Low    |
| Unreferenced top-level dirs (2026-01-29-00) | ~4.6 MB      | Low    |
| Other unreferenced dirs (older snapshots)   | ~2.5 MB      | Low    |
| Unreferenced `adgn/src/adgn/` subdirs       | ~10 MB       | Medium |
| **Total remaining**                         | **~21 MB**   |        |
