# Agent: knowledge-garden maintainer (K1-K5)

Evaluated: Quartz, Obsidian(+Dataview/Datacore+Publish), Logseq, Foam, Dendron,
TiddlyWiki, Athens, AFFiNE, SiYuan, and SilverBullet (surfaced during research, not in
the original candidate list).

| Tool                                                     | Git-backed                                                        | Live dynamic components                                                                                           | Agent-authorable     | Self-hosted UI        | Maintenance                        |
| -------------------------------------------------------- | ----------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | -------------------- | --------------------- | ---------------------------------- |
| **Obsidian + Datacore/DataviewJS, published via Quartz** | Yes                                                               | Yes — Datacore gives real React components with live data; DataviewJS can drive Chart.js from `fetch()`           | Yes, all plain files | Via Quartz            | Active                             |
| **SilverBullet**                                         | Yes                                                               | Yes — "Space Lua" widgets do async HTTP + render, purpose-built for this                                          | Yes, all plain files | Yes, built-in, Docker | Active, fast release cadence       |
| Quartz alone                                             | Yes                                                               | Client-side JS fetch only (`.inline.ts` hooks), no editor-time widget system                                      | Yes                  | Yes, static site      | Active (rolling, not tagged)       |
| Logseq                                                   | Yes                                                               | Custom-block render API still in beta; project mid-split into DB-backed version (data-loss risk during migration) | Yes                  | Yes                   | Uncertain — avoid for now          |
| Foam                                                     | Yes                                                               | None built-in; delegated to a separate publish theme with no dynamic-component story                              | Yes                  | Via `gatsby-theme-kb` | Low activity                       |
| Dendron                                                  | Yes                                                               | —                                                                                                                 | Yes                  | —                     | Maintenance-only, creator departed |
| TiddlyWiki                                               | Partial (Node.js/folder mode only)                                | Yes — mature widget system, charting plugins                                                                      | Yes, in folder mode  | Yes                   | Very active                        |
| Athens                                                   | Yes                                                               | —                                                                                                                 | —                    | —                     | **Dead**, exclude                  |
| AFFiNE                                                   | **No** — CRDT-native, markdown only via import/export             | —                                                                                                                 | —                    | —                     | Exclude (fails K1)                 |
| SiYuan                                                   | **No** — proprietary `.sy` block format, markdown via plugin only | —                                                                                                                 | —                    | —                     | Exclude (fails K1)                 |

**Recommendation, ranked**:

1. **Obsidian (vault) + Datacore/DataviewJS, published via Quartz** — richest
   agent-editable dynamic-component story (a real Plaid-spending-chart component is a
   Datacore/DataviewJS block, same git repo as the notes), mature plugin ecosystem,
   Quartz supplies the self-hosted graph/backlink-capable public site.
2. **SilverBullet** — purpose-built single-tool answer (Space Lua HTTP + widgets,
   self-hosted out of the box), younger ecosystem, no native graph view yet found.
3. **Quartz alone** — simplest/most durable plain-markdown store with a polished
   published UI, if "dynamic" only needs to mean client-side data fetching rather than
   an editor-time widget system.

**K5 (agent-workspace vs. garden boundary)** remains genuinely open — none of the
researched tools have an opinion on where a harness's own working state should live
relative to the garden repo; this is a modeling decision for later, not something the
tool choice resolves.
