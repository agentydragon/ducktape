# TODO

- [ ] Move the egress gate to the `requestheaders` hook (before mitmproxy dials the
      upstream) so request-body streaming / `stream_large_bodies` can be enabled without a
      fail-open window; until then request bodies stay buffered. Prerequisite for #4670
      broad adoption (dind layer pulls); surfaced in #4914/#4967.
- [ ] Forgejo fence activation (#4710·1 follow-up):
      `cluster/k8s/agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml` opens the
      sandbox's DIRECT egress only to the internal `forgejo-http.forgejo:3000`, not the
      public `git.allegedly.works` (anti-hairpin). Correct today — the public origin is
      reached through the fence (the Console pod's own egress), matching the
      `egress_decide` entry — but an activation that routes the sandbox to the public
      host directly needs this CCNP revisited.
