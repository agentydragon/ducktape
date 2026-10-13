"""The API group these CRDs live in, spelled once; also the domain of every Agentplane label and annotation.

Five kinds in one group, defined in `agentplane/crds/`: `EgressCredential`, `PolicySet`,
`PolicyBinding`, `SandboxTemplate` and `Sandbox`.
Several services watch them, and a group or version that disagreed between any two of those readers
would be a watch that returns nothing rather than an error anyone sees. Label and annotation keys are `{GROUP}/<name>`, or a
subdomain of it for one component's own keys (`app.{GROUP}`, `sandbox-actions.{GROUP}`).
"""

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
