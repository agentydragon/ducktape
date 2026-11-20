# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Agentplane test architecture:** [current branch plan](agentplane-test-architecture/index.html),
  [fixture ownership](agentplane-test-architecture/index.html#pytest-fixtures),
  [current evidence](agentplane-test-architecture/evidence.json),
  [source manifest](agentplane-test-architecture/manifest.json). All four native architecture ports are merged: explicit App fixtures (#16675), SVID clients (#16689), dependency-based suite partitions (#16695) and validated trust-Pod reuse (#16700). Compares the rebased draft #16489 with devel: only manual comparison targets and experiment records remain. Runtime JUnit confirms Pod 16+7 and MCP/central 1+3+4 case inventories. Historical selector, rejected shard and two/three/four-worker cohorts retain their original source pins. The separate four-worker port #16659 measured 289.2s versus 363.3s at two workers in four matched warm 22-case actions; a 30% complete-feedback improvement remains unqualified.

- **CI test cost:** [test and fixture audit](ci-test-cost/index.html), with a bounded fresh-execution sample and remaining test-selection, fixture-reuse, and setup investigations. [Evidence](ci-test-cost/evidence.json); [manifest](ci-test-cost/manifest.json).

- **Latency:** [current report](latency/index.html): Firecracker fixture reuse and a two-worker KVM minimum are deployed. Fresh acceptance passed; warm setup was 40.6s versus 106.3s in its cold seed. Whole-PR latency, fleet reuse rate and the warm-floor benefit have not been measured. The [paired VM comparison](latency/index.html#vm-shadow) found KVM first attempts were a median 4m28s shorter across 100 manual pairs. KVM also ran faster in all four fresh devel pairs collected before the shadow was retired. Earlier experiments retain their original windows. [Evidence](latency/evidence.json); [manifest](latency/manifest.json).

- **Reliability (10–16 November UTC):** [report](reliability/index.html),
  [evidence](reliability/evidence.json), [manifest](reliability/manifest.json).
  Mainline: 32 failures / 883 completed test invocations, plus six disconnected.
  VM cache and physical-attempt audit, a full 375-second setup breakdown, the
  interim 380-fresh-result baseline census for #16027, three
  red-at-merge devel PRs, and current dispositions of earlier interventions.
  The 17 November follow-up records merged case accounting, a pre-Runtime kernel stall and cleanup fence, native profiles around 25 cold imports, bounded browser snapshot controls, and a focused VM topology control. The [paired VM comparison](reliability/index.html#vm-shadow) found 23 managed and two KVM first-attempt failures across 100 manual pairs. Both routes passed all four fresh devel pairs collected before the shadow was retired. Provider snapshot decisions remain unavailable. Historical measurements retain their original windows.

- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with upstream status, local impact, evidence, and follow-up actions.

- **Cluster storage:** [HDD budget scenarios, volume fullness and interventions](storage/index.html), with dated Langfuse, SeaweedFS, ClickHouse, Zot, Mimir and Loki evidence and the earlier inventory retained below it.
- **Cluster VPA:** [coverage, resource requests and action batches](vpa/index.html), based on the 20 November cluster snapshot; includes a searchable workload inventory, OOM evidence, cohort-based rollout plan, shared VPA configuration and a map of Kyverno policy responsibilities and simpler alternatives. Rollout and admission checks at 09:10, 09:21 and 09:47 UTC are linked from the [rollout section](vpa/index.html#verified-rollouts). A [10:04 UTC follow-up](vpa/index.html#rollout-follow-up) records the API proxy, GitLab Shell, and MCP/Plaid rollouts. The [10:38 UTC update](vpa/index.html#rollout-wave) adds admitted memory requests and verifies disruption budgets and webhook replicas. The [11:00 UTC follow-up](vpa/index.html#adoption-progress) records later KEDA and Kyverno changes and refreshes the retirement gates. The [retirement status](vpa/index.html#kyverno-retirement) records the Gateway and revision-history cutovers. The [11:34 UTC cert-manager check](vpa/index.html#cert-manager-ha) records controller and CA injector availability. The [17:57 UTC follow-up](vpa/index.html#controller-ha) verifies Crossplane and metrics-server replicas and records the Sidekiq capacity risk. The [staging rollout and 18:03 UTC admission checks](vpa/index.html#agentplane-staging-initial) cover eight explicit Initial VPAs. [Evidence](vpa/evidence.json); [manifest](vpa/manifest.json).
- **Fixture and model audit:** [open findings](fixture-model-audit/index.html), with current acceptance criteria and source provenance.
- **Agentplane organization:** [whole-Agentplane current and goal trees](agentplane-organization/index.html#trees) with every tracked Go file's disposition and every package's responsibility, goal owner and rationale. Remaining work only, ordered by certainty: agreed service moves, moves waiting on open service decisions, and deferred contracts; plus the [keep-alive hold design](agentplane-organization/index.html#keep-alive). [Remaining plan](agentplane-organization/go-plan.json); [file ledger](agentplane-organization/go-files.json).
- **Agentplane protocol shapes:** [open criteria](agentplane-protocol-shapes/index.html), with service ownership boundaries and pinned source evidence.

Each CI refresh replaces its own directory's `index.html`, `evidence.json`,
`manifest.json`, and optional `attribution.json`, through a PR against `gh-pages`.
Preserve the other report directories; keep this navigation and the root index
current when report entries change. Remove stale optional
attribution only within the report being refreshed. Use Git history for earlier
measurements; do not accumulate dated run directories. This branch continues the
history of `ci-latency-history`; pre-migration latency snapshots are at the root
of their historical commits. GitHub Pages serves this branch at
<https://agentydragon.github.io/ducktape/> (root `index.html` lists the reports;
`.nojekyll` keeps Pages from processing the files).

Collection and publication tools remain on
[devel](https://github.com/agentydragon/ducktape/tree/devel/devinfra/ci/skills/ci_latency).
Keep raw logs, profiles, webhook payloads and credentials out of this branch.

## Presentation convention

Use **Mantine 9.6.1**, the version resolved on the inspected devel
branch, with React / React DOM 19.3.0. Use the version-pinned stylesheet from
jsDelivr (with its integrity hash) and the pinned esm.sh import map already in each
page. Reuse the shared `report.js` and `report.css`; do not introduce a second
framework or copy a report-specific design system. Update the entire site together
when changing the common framework. The former root used Pico; Mantine is now the
site-wide convention.

Author explicit React/Mantine components using the pinned `htm` tagged-template
library, imported through the shared `report.js`. Call `mount(Report)` with the
report component; the shared provider uses `defaultColorScheme="auto"`. Use normal
Mantine components for layout, cards, typography, controls, tables and disclosures.
Keep interactive state in React. Do not add a runtime HTML-to-component adapter,
duplicate report markup or reimplement component styling in shared CSS.

Use **Mantine Charts 9.6.1** for ordinary statistical charts, with its pinned
stylesheet after core styles and the existing pinned Recharts import-map entry.
Keep counts and units visible, provide accessible text or tables for the values,
and retain observation windows and denominators. Preserve useful visualizations
when refreshing a report. Custom SVG is appropriate for nested wall-time span
timelines; their intervals overlap and are not ordinary additive data series.

The head sets the initial system color scheme; the provider follows subsequent
browser/OS changes. Shared CSS contains only the span timeline's SVG styles;
use component props for layout and appearance. Do not force light or dark.
Before publishing, render every affected page at desktop and mobile widths in both
themes, verify real CDN loading and live preference changes, and exercise its
controls. Inspect the screenshots for meaningful bars, readable labels and retained
visual distinctions; successful browser assertions alone are insufficient.
A presentation refresh must retain the original measurement/source
window and must not imply that old findings were remeasured.

## Reviewing paired VM runs

Automatic managed TCG shadow admission was disabled on 20 November 2026 at 07:36 UTC. The archived comparison includes the 12-pair and 100-pair manual batches and the two devel cohorts on either side of the KVM routing cutover. Required VM tests run on recycled cluster KVM. [Per-pair evidence](vm-pairs.json) · [KVM routing PR](https://github.com/agentydragon/ducktape/pull/18046) · [Shadow removal PR](https://github.com/agentydragon/ducktape/pull/18079)

The [original collector](https://github.com/agentydragon/ducktape/pull/17940) launched the recycled KVM shadow after a fresh managed result. Version 1 archives store KVM results in `shadow/`. After the cutover, version 2 launched the managed TCG shadow after a fresh required KVM result. Its `pair.json` records `required_route` and `shadow_route`, and its physical routes use `managed/` and `kvm/`.

Historical artifacts were named `vm-pair-RUN_ID-RUN_ATTEMPT` and requested 90 days of retention. For a review of an archived pair, join on run ID, attempt, and tested SHA. The target, execution and case records distinguish cached results, executor retries, Bazel retries and failed first attempts. The archived reports use native queue clocks only when enqueue and worker-start timestamps are ordered. [Version 1 run](https://github.com/agentydragon/ducktape/actions/runs/44660636046) · [Version 2 fresh pair](https://github.com/agentydragon/ducktape/actions/runs/44688973875/artifacts/14607984419)
