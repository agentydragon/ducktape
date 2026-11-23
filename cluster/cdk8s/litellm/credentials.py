"""The Crossplane-owned LiteLLM cheap-experiments key, which Agentplane's egress proxy presents."""

from cluster.cdk8s.litellm.endpoint import NAMESPACE
from cluster.cdk8s.secret_ref import SecretRef

CHEAP_EXPERIMENTS_KEY = SecretRef(namespace=NAMESPACE, name="litellm-key-cheap-experiments").key("api-key")
