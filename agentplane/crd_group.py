"""The API group these CRDs live in, spelled once.

Five kinds in one group, defined in `agentplane/crds/`: `EgressPolicy`, `EgressBinding`,
`EgressCredential`, `ActionPolicySet` and `ActionPolicyBinding`. Several services watch them, and a
group or version that disagreed between any two of those readers would be a watch that returns
nothing rather than an error anyone sees.
"""

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
