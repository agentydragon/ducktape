# GitHub Pages

`gh-pages` is the publication branch for
<https://agentydragon.github.io/ducktape/>. It holds independent, latest-only
report snapshots. The CI reports are:

- **Agentplane test architecture:** [current branch plan](agentplane-test-architecture/index.html),
  [fixture ownership](agentplane-test-architecture/index.html#pytest-fixtures),
  [current evidence](agentplane-test-architecture/evidence.json),
  [source manifest](agentplane-test-architecture/manifest.json). All four native architecture ports are merged: explicit App fixtures (#16675), SVID clients (#16689), dependency-based suite partitions (#16695) and validated trust-Pod reuse (#16700). Compares the rebased draft #16489 with devel: only manual comparison targets and experiment records remain. Runtime JUnit confirms Pod 16+7 and MCP/central 1+3+4 case inventories. Historical selector, rejected shard and two/three/four-worker cohorts retain their original source pins. The separate four-worker port #16659 measured 289.2s versus 363.3s at two workers in four matched warm 22-case actions; a 30% complete-feedback improvement remains unqualified.

- **CI test cost:** [test and fixture audit](ci-test-cost/index.html), with a bounded fresh-execution sample and remaining test-selection, fixture-reuse, and setup investigations. [Evidence](ci-test-cost/evidence.json); [manifest](ci-test-cost/manifest.json).

- **Latency:** [current report](latency/index.html): Firecracker fixture reuse and a two-worker KVM minimum are deployed. Fresh acceptance passed; warm setup was 40.6s versus 106.3s in its cold seed. Whole-PR latency, fleet reuse rate and the warm-floor benefit have not been measured. The [paired VM comparison](latency/index.html#vm-shadow) found a 5m42s median KVM saving across 11 complete manual pairs and shorter KVM attempts on all three fresh devel pushes. Earlier experiments retain their original windows. [Evidence](latency/evidence.json); [manifest](latency/manifest.json).

- **Reliability (10–16 November UTC):** [report](reliability/index.html),
  [evidence](reliability/evidence.json), [manifest](reliability/manifest.json).
  Mainline: 32 failures / 883 completed test invocations, plus six disconnected.
  VM cache and physical-attempt audit, a full 375-second setup breakdown, the
  interim 380-fresh-result baseline census for #16027, three
  red-at-merge devel PRs, and current dispositions of earlier interventions.
  The 17 November follow-up records merged case accounting, a pre-Runtime kernel stall and cleanup fence, native profiles around 25 cold imports, bounded browser snapshot controls, and a focused VM topology control. The [paired VM comparison](reliability/index.html#vm-shadow) found two managed first-attempt failures and no KVM failures in the manual batch. Both routes passed three fresh devel push pairs. Provider snapshot decisions remain unavailable. Historical measurements retain their original windows.

- **Visual reliability:** [standalone HTML](visual-reliability/index.html),
  [evidence](visual-reliability/evidence.json),
  [source and observation window](visual-reliability/manifest.json).

- **Upstream patches and issues:** [reviewed inventory](upstream-patches/index.html), with upstream status, local impact, evidence, and follow-up actions.

- **Cluster storage:** [HDD budget scenarios, volume fullness and interventions](storage/index.html), with dated Langfuse, SeaweedFS, ClickHouse, Zot, Mimir and Loki evidence and the earlier inventory retained below it.
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

On every latency or reliability review, update the paired VM section from the latest available [main CI runs](https://github.com/agentydragon/ducktape/actions/workflows/ci.yml?query=branch%3Adevel+event%3Apush). The [collector](https://github.com/agentydragon/ducktape/pull/17940) runs on devel pushes. A fresh first result for the managed VM suite triggers the recycled KVM variant at the same SHA. Each suite gets at most two Bazel attempts. Publishing follows the regular report PR process. The [per-pair measurements](vm-pairs.json) also preserve the 12-pair manual batch on one pinned commit. Refresh the push cohort alongside that fixed comparison. The 50- and 200-pair checkpoints count fresh push pairs only.

1. Enumerate devel push runs and their attempts since the last reviewed cutoff, including failed, cancelled and still-running jobs. Revisit previously incomplete pairs. Download each available artifact with `gh run download RUN_ID -R agentydragon/ducktape -n vm-pair-RUN_ID-RUN_ATTEMPT -D OUTPUT_DIR`. Archives request 90 days of retention. Record missing, expired, incomplete and cached selections explicitly.
2. Join by GitHub run ID, run attempt and tested SHA in `pair.json`. Check both invocation SHAs, label and first-result cache evidence in each route's `invocation.json` and `target.json`. Verify Claude and Codex coverage in the per-attempt `cases.json`. Keep missing cases visible when a suite fails before JUnit is written. Check `executions.json` for infrastructure retries or shared executions, `fixture.json` for cold/warm state and `collection-errors.json` for partial archives. Deduplicate shared executions and identify repeated SHAs. Each eligible pair contributes once.
3. For reliability, tabulate both passed, only managed failed, only KVM failed and both failed on **attempt one**. Report recovery among actual retries and final failure among complete pairs separately. Inspect failure signatures, including shared regressions. Report failures before a test result and shadow launch failures against all eligible opportunities. Include uncertainty and clustering by commit and day when revising the routing recommendation.
4. For latency, compare within-pair queue, preparation, test execution and time through final verdict using valid timestamp pairs. Break out retries, cold/warm state and missing or inverted clocks. The shadow launches after the managed first result and shares pool capacity with required Firecracker tests. Measure contention and the required-check critical path before estimating a PR feedback benefit.
5. In each report, refresh the reviewed cutoff, source revision, linked artifacts/runs, denominators, results and recommendation. Reconcile shared counts at the same cutoff. At 50 fresh pairs check selection and missingness; at 200 evaluate routing using paired outcomes, uncertainty, retry cost and pool contention. Earlier synthetic canaries and historical unpaired mainline observations retain their own denominators.
