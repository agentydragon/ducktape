# TODO

- [ ] Move the egress gate to the `requestheaders` hook (before mitmproxy dials the
      upstream) so request-body streaming / `stream_large_bodies` can be enabled without a
      fail-open window; until then request bodies stay buffered. Prerequisite for #4670
      broad adoption (dind layer pulls); surfaced in #4914/#4967.
