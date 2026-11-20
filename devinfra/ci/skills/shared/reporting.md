# Shared CI report publishing contract

## PR dashboard scope

Refresh the broad PR sample even when the newest work concerns one incident or
rollout. Open with the headline metric and its daily trend, normally for the
last seven complete UTC days. Show daily denominators, flag partial days and
policy changes, and leave unobserved periods as gaps. If collection is incomplete,
show the gap and date any retained baseline. Quantify the impact of a driver or
proposed fix in PR terms, such as minutes per affected head or reruns per week.
Check recommendations against current source, routing and landed fixes.

A landed change can still be an open measurement question. Keep a compact view
after the PR overview while the reader needs to judge its benefit, recurrence
or rollout safety. Show the affected PRs or tests, the baseline, and comparable
post-change observations so far. For a flake fix, for example, report failures
per fresh execution on both sides. Include the exposure or observation window
needed for a decision and what result would warrant action. Update that view
in place. Once the question is answered or ceases to affect a decision, fold
the result into the current findings and move supporting history into disclosure.

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
Use semantic headings, accessible controls and chart labels that work without color.
Review the full page on desktop and mobile with disclosures closed. Verify actual CDN loading, both
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
