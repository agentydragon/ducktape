# TODO — Agentplane egress proxy

## Give agents scoped Authentik inspection through Agentplane

- [ ] Decide whether agents need one or more narrowly scoped Authentik service accounts for
      inspection, then make their credentials available through Agentplane credential
      substitution in the egress proxy (<SPEC.md>). Define the endpoint and permission scope,
      and keep raw Authentik tokens out of agent-readable Kubernetes Secrets.
