# Shared CI report publishing contract

Read this reference when authoring or publishing either CI report.

## Presentation

Before changing a report, read the current `gh-pages` root `README.md`, shared
assets and sibling pages; reuse the framework and shell they document. The current
site convention uses pinned CDN assets, React, Mantine and the shared `report.js`
and `report.css`. Use `MantineProvider` with `defaultColorScheme="auto"` and
Mantine's typography provider unless the current branch documents a change.
Preserve report controls and limit custom CSS to report-specific layout, charts
and small adjustments. Author actual standalone HTML; do not wrap Markdown in a
preformatted block. Verify actual CDN loading, desktop and mobile layouts, both
initial color schemes and a live OS or browser preference change. Keep
styling-only work distinct from measurement or source updates.

Let the findings determine the report structure; keep conclusions and decisions
easy to find, with semantic headings, accessible controls and useful tables or
visualizations. Label units, populations and sample sizes, and place important
limitations by the claims they qualify.

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
