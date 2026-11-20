---
name: action_report
description: Write or refresh a report or dashboard that helps its reader assess current conditions, understand the main drivers and decide what to try. Use for selecting metrics, graphs and evidence links, ranking interventions, and editing an existing report as a whole.
---

# Action-oriented reports

Identify the reader and the decision this report should inform. Choose the
measurements that would change their judgment. Let that question determine the
graphics, level of detail and recommendations.

## Default reading order

1. **Current state.** Put the answer at the top with the few metrics and visuals
   that establish it. A trend shows change over comparable windows. A distribution
   shows how widely experiences vary. Put units, population, sample size and
   observation window beside the numbers. Label gaps where they affect the answer.
2. **What explains it?** Show where the cost or failures concentrate. Compare
   groups with their denominators and quantify the contribution of important
   drivers. Use a concrete case or trace to explain a mechanism after establishing
   its reach. Link directly to the evidence for a finding. Put methods and
   exhaustive tables within reach of readers who want to check it.
3. **What should we do?** Rank the worthwhile interventions. For each, say what
   would change, who or what would benefit, by how much, and at what cost.
   Distinguish measured effects from estimates and upper bounds. If the payoff
   is uncertain, give the smallest experiment that could change the decision.
   If the evidence favors keeping the current arrangement, say why.
4. **What happened previously?** Include history when it explains a current
   decision, such as a failed trial that rules out an otherwise promising fix.
   Put it last, behind a disclosure on a dashboard or in a linked appendix.

## Refresh the whole report

Read the existing report before inserting a finding. What decision changes in
light of the new evidence? Update the headline, comparisons and ranked actions
where the answer changes. An experiment that establishes no new conclusion can
stay in the evidence. Preserve the observation date of a baseline until it is
remeasured. When one study informs several reports, draw the relevant conclusion
for each audience and link to a single account of the method and results.

Read the finished report from the top in the form the audience will see, with
disclosures closed. Can they judge the current state, find the main driver and
decide what to try without reading an investigation diary? Cut sections that
do no work for that reader, combine repeated claims, and shorten prose that
only narrates a graph. Keep useful evidence links working when moving detail.
Check the title, summary, charts, captions and recommendations for assumptions
superseded by the latest finding.

## Required before handoff: render and inspect

Render the exact deliverable after the last edit, using the reader's entry point
and a viewer appropriate to the format. For a web dashboard, open the served
report in a real browser with its scripts, styles and data. With Playwright,
start collecting page errors before navigation so a syntax error during startup
is caught. Wait for report-specific content such as a populated headline and
chart. Exercise changed filters or disclosures and check for runtime errors and
failed loads of required assets. For a document, open the final export in its
viewer and inspect the pages.

Look at the rendered output yourself, directly or in screenshots from that run.
Check that the figures show data, labels are legible, and the layout works at
the sizes readers use. Ducktape web reports with visual tests can use the
[visual test harness](https://github.com/agentydragon/ducktape/blob/devel/testing/frontend_visual/README.md)
and inspect the captures published by PR CI.

Fix failures and repeat the render and inspection before calling the report
ready. If rendering is blocked, report the blocker and mark the review incomplete.
At handoff, identify the artifact or revision you viewed, what you checked, and
any remaining limitation. Link the preview or captures when available.
