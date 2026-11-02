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

Diagnose visual-test timeouts during capture and after screenshots were written. Instrument
browser and container startup, rendering and teardown so a timeout identifies where progress
stopped. Measure setup costs, then evaluate sharing Grafana or a browser or combining
screenshots into one test, including effects on isolation, parallelism, caching and
per-dashboard visual baselines.

## E. Choose a fixture strategy

Keep both Piraeus scenarios until a decision is made. Compare recorded responses with
queries against a synthetic Prometheus for runtime, fixture authoring and maintenance,
representative output and query coverage. Exercising dashboard queries is appealing; test
that approach on a more complex dashboard and account for Loki and ClickHouse. Choose one
default approach and document any justified exceptions.

## F. Dashboard declaration and cdk8s

Prototype a Bazel dashboard declaration whose JSON can feed both cdk8s manifest generation
and its visual test. Try a monitoring dashboard and a component-owned dashboard; keep Flux
resource ownership and datasource wiring explicit. Evaluate whether it eliminates
duplicated declarations without making customization or deployment less clear.

## G. Dashboard and fixture layout

Choose and apply a consistent naming and placement convention for dashboard JSON, fixtures
and test targets. Monitoring dashboards have named JSON files and `visual_fixtures/`, while
GitHub and gateway dashboards have generic filenames beside their component generators.
Decide how component ownership affects placement. Keep reusable test machinery under
`//grafana/visual_tests` and dashboard-specific data with its owner.

## I. Exact-base visual comparisons

Make before-and-after images use the exact PR base and head with the same fixtures and
capture settings, or clearly identify a fallback baseline. New targets currently have no
before image until a baseline has been published. Track the remaining work in
[issue #13556](https://github.com/agentydragon/ducktape/issues/13556).

## J. Manual live-data preview

Design an explicitly launched, trusted workflow that renders a candidate against real
metrics. Decide who can launch it, credential scope, network access, artifact retention and
where images can be viewed. Do not automatically publish production-derived screenshots
to the public PR visuals gallery.
