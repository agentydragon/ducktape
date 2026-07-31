# Findings

Numbered in discovery order across the whole programme and cited by number from
cluster manifests, so the IDs are stable — which is why they are not contiguous
within a file. Grouped here by subject.

| #   | Finding                                                                         |                                           |
| --- | ------------------------------------------------------------------------------- | ----------------------------------------- |
| F1  | A second supervisor invocation breaks the sandbox's SSH relay, permanently      | [openshell](openshell.md)                 |
| F2  | Sandbox egress policy is per-process, not per-pod                               | [openshell](openshell.md)                 |
| F3  | Operator-managed OpenClaw cannot be egress-confined                             | [openshell](openshell.md)                 |
| F4  | A domain allowlist that works, without Cilium or the shared mitmproxy           | [egress and TLS](egress_and_tls.md)       |
| F5  | The workspace git repo is real but never used                                   | [harness behaviour](harness_behaviour.md) |
| F6  | S2 passes once a GitHub credential exists                                       | [credentials](credentials.md)             |
| F7  | `GITHUB_TOKEN` is stripped from the exec tool by an exact-name denylist         | [credentials](credentials.md)             |
| F8  | mitmproxy re-keys its CA on restart, and the agent turns TLS verification off   | [egress and TLS](egress_and_tls.md)       |
| F9  | Memory embedding search was never configured                                    | [harness behaviour](harness_behaviour.md) |
| F10 | A credential-injecting proxy works, and closes F7's exposure                    | [credentials](credentials.md)             |
| F11 | The whole harness cannot run under OpenShell on the k8s operator                | [openshell](openshell.md)                 |
| F12 | Docker-in-Kubernetes is viable; k3d gives us a local rig                        | [tooling](tooling.md)                     |
| F13 | OpenClaw does run inside an OpenShell sandbox on the Docker driver              | [openshell](openshell.md)                 |
| F14 | Every config-driven pod restart breaks the session that was live                | [harness behaviour](harness_behaviour.md) |
| F15 | iron-proxy expresses our whole credential policy declaratively                  | [egress and TLS](egress_and_tls.md)       |
| F16 | Real OpenClaw behind iron-proxy: confinement holds, a PR goes out end to end    | [egress and TLS](egress_and_tls.md)       |
| F17 | `GIT_SSL_CAINFO` is stripped, and git links GnuTLS so no other CA var covers it | [egress and TLS](egress_and_tls.md)       |
| F18 | Node ignores a missing `NODE_EXTRA_CA_CERTS` silently, with a misleading error  | [egress and TLS](egress_and_tls.md)       |
| F19 | OpenClaw discards the declaratively seeded config after any gateway-side write  | [harness behaviour](harness_behaviour.md) |
| F20 | Langfuse receives the traces; its read API aborts on modest queries             | [tooling](tooling.md)                     |

Also here: [rough edges, knowns and unknowns](rough_edges.md).

## The three that changed decisions

- **F3** ruled out the operator entirely — its NetworkPolicy always emits an
  unconditional 443 egress rule, and NetworkPolicies are unions of allows, so it
  cannot be subtracted. That is why `public-coder-agent` is a plain Deployment.
- **F7** explains why the agent could not authenticate to GitHub for weeks, and
  **F10** shows the fix that supersedes it: the credential does not have to be in
  the agent at all.
- **F8** is the cautionary one. A broken TLS trust chain led the agent to disable
  verification and carry on silently, which says more about designing for agents
  than the CA bug does.
