# Agentplane LLM workload ingress

This is the authenticated Sandbox-facing hop in front of the existing LiteLLM deployment. The
central egress proxy substitutes the caller's already-authenticated Pod-bound workload token into
an ordinary `Authorization: Bearer` header. This service resolves that bearer with the shared
`SandboxPrincipalAuthenticator`, removes it, and forwards the request to LiteLLM with one
server-held virtual key.

The forwarded byte body, status, error body, and streamed chunks are not translated. Verified
identity is attached only through LiteLLM 1.90.2's documented
`x-litellm-spend-logs-metadata` JSON header. That version consumes the header in its common request
setup used by both native `/v1/messages` and `/v1/responses`, including their streaming paths. The
authoritative metadata object is:

```json
{
  "agentplane.namespace": "...",
  "agentplane.service_account": "...",
  "agentplane.service_account_subject": "...",
  "agentplane.pod_name": "...",
  "agentplane.pod_uid": "...",
  "agentplane.sandbox_name": "...",
  "agentplane.sandbox_uid": "..."
}
```

Incoming LiteLLM metadata/customer/agent headers and Agentplane/Sandbox/Pod/Agent/Thread identity
headers are removed before this object is stamped. Request bodies remain provider-native and are
never identity evidence; a caller's body `metadata`, Agent, Thread, Pod, or Sandbox fields cannot
replace the server-stamped object.

The workload bearer is sent only to Kubernetes TokenReview. The LiteLLM virtual key is sent only on
the internal LiteLLM hop. Neither credential is logged, placed in errors, returned to callers, or
mounted into a runner or harness container.
