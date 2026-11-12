# TODO — Agentplane egress proxy

## Give agents scoped Authentik inspection through Agentplane

- [ ] Decide whether agents need one or more narrowly scoped Authentik service accounts for
      inspection, then make their credentials available through Agentplane credential
      substitution in the egress proxy (<SPEC.md>). Define the endpoint and permission scope,
      and keep raw Authentik tokens out of agent-readable Kubernetes Secrets.

## Evaluate upstream connection reuse

Benchmark pooled `net/http` transports against the per-request transport in `proxy/proxy.go`.
Preserve per-request authentication and policy checks, DNS address pinning at the actual dial,
and the existing streaming, revocation, and WebSocket behavior. Use the
[Iron reuse spike](https://github.com/agentydragon/ducktape/pull/10980) as comparison input before
choosing a transport replacement; measure the standard-library option first.
