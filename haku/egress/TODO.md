# TODO

- [ ] Move the egress gate to the `requestheaders` hook (before mitmproxy dials the
      upstream) so request-body streaming / `stream_large_bodies` can be enabled without a
      fail-open window; until then request bodies stay buffered. Prerequisite for #4670
      broad adoption (dind layer pulls); surfaced in #4914/#4967.
- [ ] Forgejo placeholder completion (#4710·1 follow-up; runtime `no_proxy` is flipped —
      the CLI's Forgejo traffic traverses the fence, carrying the real credential from the
      bootstrap's ~/.netrc, passed through unsubstituted): teach the baked
      `haku-sandbox-setup.sh` to clone via the fence (bearer-userinfo proxy URL from
      `HAKU_RUNNER_TOKEN` + the mounted fence CA) so the SandboxTemplate can inject
      `haku-forgejo-token-placeholder` instead of the real `haku-forgejo-git` secret —
      script first (backwards-compatible no-op without the env), template flip after the
      image converges. CCNP caveat stands:
      `cluster/k8s/agents/haku-egress-proxy/ccnp-haku-agent-egress.yaml` opens the
      sandbox's DIRECT egress only to the internal `forgejo-http.forgejo:3000`, not the
      public `git.allegedly.works` (anti-hairpin) — fine while all runtime traffic uses
      the fence; an activation routing the sandbox to the public host directly needs it
      revisited.
