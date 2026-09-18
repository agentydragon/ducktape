# Per-key model allowlists ({provider}/{shape}/{model} names, cluster/cdk8s/model_rosters.py),
# one lane per key. Set by the generated Terraform CR
# (cluster/k8s/litellm/keys-tf/litellm-keys.k8s.yaml) from cluster/cdk8s/litellm_keys.py,
# which documents each lane and refuses any name the generated LiteLLM config does not serve.
variable "model_allowlists" {
  description = "Per-key LiteLLM model allowlists, one lane per key, from cluster/cdk8s/litellm_keys.py via the generated Terraform CR"
  type        = map(list(string))
}
