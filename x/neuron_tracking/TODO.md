# TODO

- [ ] Extend the reviewed frame-52 examples to short identity tracks and nearby
      excluded structures before another full-clip run. Add more checks as those
      examples are reviewed; the accepted count, body locations, and distinct IDs are in
      `sample_checks.json`.
- [ ] Extend shared validation to CSV consistency and visual review coverage of
      the exported annotation revision. The renderer already validates source
      identity, timestamps, masks, ID colors, and decoded video dimensions/count.
- [ ] Compare the five runs against source pixels: missed targets and branches,
      background leakage, body/branch boundaries, and identity drift. Use their
      native Codex traces for ad hoc investigation.
- [ ] Improve target coverage and tracking through pose changes. A full set of
      frame records does not establish a complete neuron census or correct IDs.
      Keep unresolved candidate groups separate from individual neuron identities.
- [ ] Strengthen visual review so drift and masks outside the specimen trigger
      corrections. Require native-resolution comparisons at difficult transitions
      and recheck changed annotations before export.
- [ ] Pave a durable launch/resume procedure for long parallel Codex jobs and
      investigate their unexpected SIGTERM exits. Preserve each run's existing
      conversation, progress files, and native trace across restarts.

Compaction is allowed. Keep each run in one Codex conversation and persist the
identity table, annotation revision, unresolved decisions, and review progress so
the agent can continue reliably after compaction.
