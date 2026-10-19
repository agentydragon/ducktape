# TODO

- [ ] Add a shared renderer that takes the source AVI and `annotations.json` and
      produces the annotated AVI. Use the saved ID/color table and pixel runs,
      distinguish body, branches, and uncertainty, and keep labels readable.
      Check source identity, dimensions, frame order, and output timing; report
      any AVI timing compromise or added frames explicitly. Support still-frame
      and per-layer previews through the same rendering code used for the video.
- [ ] Add shared validation for schema and semantic mask invariants, source
      timestamps, CSV consistency, and the exported video's relationship to the
      reviewed annotation revision. Exercise it with synthetic pixels and media.
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
