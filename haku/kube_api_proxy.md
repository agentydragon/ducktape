# Retired Haku Kubernetes API proxy

The Haku-specific proxy was a Go reverse proxy that accepted a Haku bearer,
asked Haku Console to authorize Kubernetes requests, and forwarded allowed
requests with its in-cluster ServiceAccount identity. Public Coder reached it
through Agentplane egress.

PR [#11466](https://github.com/agentydragon/ducktape/pull/11466) retired the
Haku route, workload, credential wiring, and Go implementation after Console's
Kubernetes authorization endpoint had been removed. This note records the
former access path; the separate generic TLS-inspecting proxy is documented in
<../cluster/cdk8s/kube_api_proxy.md>.
