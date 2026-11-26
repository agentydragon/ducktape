"""Service SVIDs shared by Python settings and the Go transport.

Keys are the public Go constant names. Both transports trust exact URI matches.
"""

# These workload listeners can be configured as backend servers in Python.
WORKLOAD_LISTENERS = {
    "ActionsURI": "spiffe://agentplane/actions",
    "CentralSamplerURI": "spiffe://agentplane/central-sampler",
    "EgressProxyURI": "spiffe://agentplane/egress-proxy",
    "HistoryIngressURI": "spiffe://agentplane/history-ingress",
    "LLMIngressURI": "spiffe://agentplane/llm-ingress",
    "MCPEdgeURI": "spiffe://agentplane/mcp-edge",
    "NotificationsURI": "spiffe://agentplane/notifications",
    "PolicyControllerURI": "spiffe://agentplane/policy-controller",
    "SandboxControllerURI": "spiffe://agentplane/sandbox-controller",
    "SessionDispatcherURI": "spiffe://agentplane/session-dispatcher",
}

# The k3s fixture also authenticates its isolated Runtime proxy reader through a workload listener.
WORKLOAD_PEERS = {
    **WORKLOAD_LISTENERS,
    "SystemTestRuntimeProxyReaderURI": "spiffe://agentplane/system-test-runtime-proxy-reader",
}

GATEWAY = "spiffe://agentplane/runner-gateway"
SERVICES = {
    **WORKLOAD_PEERS,
    "GatewayURI": GATEWAY,
    "AppURI": "spiffe://agentplane/app",
    "EnvironmentsURI": "spiffe://agentplane/environments",
    "RuntimeProxyURI": "spiffe://agentplane/runtime-proxy",
    "SessionsURI": "spiffe://agentplane/sessions",
}
