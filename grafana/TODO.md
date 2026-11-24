# Grafana dashboard followups

Followups to the [dashboard visual-test spike](https://github.com/agentydragon/ducktape/pull/13568).
Remove an item when its decision and any resulting work are complete.

## B. Detect fixture drift

Recorded responses are selected by panel ID and query reference, so a changed query can
still display an old response. Record query provenance or a fingerprint and require an
intentional fixture review when the query changes. Preserve an explicit way to document
corrected capture queries. Require a reason for intentionally empty results; never supply
invented fallback data.

## C. Capture and refresh representative fixtures

After assessing Grafana's exporter and image renderer, choose how to capture query results
and refresh dashboard-owned fixtures. Preserve useful measured shapes, values and gaps;
keep aliases consistent across panels; trim and anonymize identifying labels and logs.
Make the time window and capture corrections reviewable, and keep raw captures private.
Reuse upstream capabilities where they cover these needs.

## D. Reliability and cost

Use the existing `grafana-stages.log` in Bazel undeclared outputs to locate stalls during
startup and capture. Diagnose timeouts during capture and after screenshots were written.
Add timings or teardown diagnostics where the existing stage markers leave a gap. Measure
setup costs, then evaluate sharing Grafana or a browser or combining
screenshots into one test, including effects on isolation, parallelism, caching and
per-dashboard visual baselines. Explore reusing a running Grafana instance across
BuildBuddy VMs, including whether networking and isolation make that practical.

## E. Choose a fixture strategy

Keep both Piraeus scenarios until a decision is made. Compare recorded responses with
queries against a synthetic Prometheus for runtime, fixture authoring and maintenance,
representative output and query coverage. Exercising dashboard queries is appealing; test
that approach on a more complex dashboard and account for Loki and ClickHouse. Choose one
default approach and document any justified exceptions.

## G. Dashboard and fixture layout

Choose a consistent naming and placement convention for fixtures and visual test targets.
Monitoring fixtures use named files under `visual_fixtures/`. GitHub and gateway fixtures
use generic filenames beside their component generators. Keep dashboard-specific data with
its owner and reusable test machinery under `//grafana/visual_tests`. Dashboard JSON naming
and build-output placement already come from `//grafana:dashboard.bzl`.

## I. Exact-base visual comparisons

The [publisher](../devinfra/pr_visuals/docs/publisher.md#baseline-resolution) already prefers
the exact base and labels fallback comparisons. Track the remaining work to obtain exact
base/head captures with the same fixtures and settings in
[issue #13556](https://github.com/agentydragon/ducktape/issues/13556).

## J. Manual live-data preview

Design an explicitly launched, trusted workflow that renders a candidate against real
metrics. Decide who can launch it, credential scope, network access, artifact retention and
where images can be viewed. Do not automatically publish production-derived screenshots
to the public PR visuals gallery.
