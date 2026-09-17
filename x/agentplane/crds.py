"""The API group these CRDs live in, spelled once.

Five kinds in one group, defined in `cluster/k8s/agentplane-crds/`: `EgressPolicy`, `EgressBinding`,
`EgressCredential`, `ActionPolicySet` and `ActionPolicyBinding`. Two services and the app read them,
and a group or version that disagreed between any two of those readers would be a watch that returns
nothing rather than an error anyone sees.
"""

GROUP = "agentplane.allegedly.works"
VERSION = "v1alpha1"
