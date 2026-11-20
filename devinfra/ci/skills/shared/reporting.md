# Shared CI report publishing contract

Use this contract when planning a dashboard refresh and again before publishing.

The reader uses these pages to judge how fast and reliable required PR CI is,
decide which problems deserve attention, and choose what to try given the likely
benefit and cost. Start each refresh by asking which decision the evidence can
inform. Choose metrics and visuals for that decision. A trend answers whether
conditions changed, a distribution shows who sees a long wait, and a breakdown
can show where failures concentrate. Omit a chart if it adds no information.

## Default reading order

1. **How are PRs doing?** Open with the headline PR metrics and a graph showing
   their distribution or trend. Put the observation window, population, sample size
   and units beside them. Refresh the broad PR sample even when the newest work
   concerns a single incident or rollout. If collection is incomplete, identify
   the gap in the opening and date any retained baseline. A new experiment does
   not refresh the baseline's date.
2. **Where is the problem concentrated?** Compare PR classes, gates or failure
   mechanisms as the evidence warrants. Show how often the driver occurs, which PRs
   it affects and how much it costs them. Use a breakdown to establish its scale
   before drilling into a representative run. Keep detailed traces and methods
   one click away.
3. **What should we try next?** Rank the open interventions. For each, name the
   affected test, trigger or service, the proposed change, expected payoff for PRs, implementation cost,
   evidence and uncertainty, and the smallest check that would support adopting it.
   Express payoff in units the reader can use, such as minutes per affected head or
   reruns avoided per week. If the evidence supports only a bound, give the bound.
   If payoff is unknown, identify the measurement that would establish it. Check
   each proposal against current source, routing and landed fixes.
4. **What changed?** Put useful history last, collapsed by default. Keep a past
   intervention when its measured effect, unresolved outcome or failed experiment
   informs a decision today. Link to dated evidence for the rest. On each refresh,
   retire superseded advice.

## Edit the whole report on every update

Even for a one-result follow-up, read the existing page and ask what this result
changes for a PR author. Revise the headline, driver breakdown or ranked actions
where the answer changes. A result that only documents an experiment can live in
linked evidence. When one experiment informs both dashboards, give each page the
conclusion relevant to its question and link the shared method and results once.

Read the complete rendered page in order on desktop and mobile with disclosures
closed. Can a reader see PR health and its graph on opening, then find the main
driver and a worthwhile next action by scrolling? For each visible section, ask
what the reader learns there that affects that judgment. Combine repeated claims,
shorten explanations the graph already supplies, and move background into a
disclosure or a dated evidence link. Preserve a useful deep link when moving detail.

Check the title, summary, charts, captions and recommendations against the same
current facts. When a fix lands or routing changes, look for every place the old
assumption shaped the advice. Keep measurement windows and material limits visible.
Use semantic headings and accessible controls. Distinguish chart series by more
than color.

## Presentation

Before changing a report, read the current `gh-pages` root `README.md`, shared
assets and sibling pages; reuse the framework and shell they document. The current
site convention uses pinned CDN assets, React, Mantine and the shared `report.js`
and `report.css`. Use `MantineProvider` with `defaultColorScheme="auto"` unless
the current branch documents a change. Prefer normal Mantine components for
layout, cards, typography, controls, tables and disclosures. Use the site's chart
framework, currently Mantine Charts, for ordinary statistical charts; reserve
custom SVG/CSS for visualizations the framework cannot express, such as nested
wall-time spans. Do not recreate component styling in shared CSS. Preserve useful
visualizations during refreshes and inspect the rendered screenshots: a passing
test can still show empty bars, clipped labels or lost distinctions. Keep original
chart windows, units and denominators when retaining historical measurements.
Author standalone HTML; do not wrap Markdown in a preformatted block.
Verify actual CDN loading, desktop and mobile layouts, both
initial color schemes and a live OS or browser preference change. Keep
styling-only work distinct from measurement or source updates.

## Evidence and privacy

Show the observation window and full inspected source SHA. Keep reviewed,
decision-relevant measurements and provenance in `evidence.json`; put sample
limits and important unknowns next to the claims they qualify. Support findings
with direct links and preserve still-relevant prior findings with commit-pinned
references that retain their original window and source. Keep raw API payloads,
logs, credentials and profiles local unless a durable, reviewed fixture needs
them. Escape external values and review public changes for identities and private
data.

## Snapshot history and publication

The canonical `gh-pages` branch holds one latest snapshot per report namespace.
Each snapshot uses `index.html`, `evidence.json` and `manifest.json`, with
`attribution.json` only when attribution was measured. If attribution was not
measured, remove any stale `attribution.json` from that namespace. Both skills
package the shared publisher as `scripts/publish.py`. It copies already-authored
HTML as-is; it does not render the report, inspect the evidence or validate the
diagnosis. Review both before using it.

Fetch the branch before publishing and submitting the report PR. Compare windows
only within the same namespace and do not replace a newer snapshot with an older
one. Integrate concurrent changes while preserving the root navigation, sibling
namespace, `.gitattributes` and unrelated files. Commit history holds prior
snapshots; do not add dated run directories. The publisher updates only the
selected namespace and removes an old `report.md` there. Report history changes
go through a PR to `gh-pages`; skill and tool changes go through a source PR to
`devel`. Link the published namespace page and say it redeploys when the PR
merges. Do not reset, amend or force-push report history.
