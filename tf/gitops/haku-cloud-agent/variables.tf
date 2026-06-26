variable "haku_kube_token" {
  type      = string
  sensitive = true
  # NOT ephemeral: the tofu-controller's spec.vars does not populate ephemeral
  # input variables, so an ephemeral var arrives null and the provider rejects
  # the credential ("auth.token: Field required"). The credential's `token` is a
  # write-only attribute, which already keeps the value out of tfstate; sensitive
  # keeps it out of logs. See main.tf rotation note.
  description = "haku-k8s Authentik JWT for the static_bearer vault credential. Set from the haku-cloud-kube-token Secret's jwt key via the Terraform CR's spec.vars."
}

variable "haku_kube_token_wo_version" {
  # String (not number): the tofu-controller passes Secret-sourced vars as strings;
  # tonumber() at the use site in main.tf.
  type        = string
  description = "The haku JWT's exp epoch (seconds). Drives the write-only token_wo_version so each rotation (new token -> later exp) re-sends the token into the Anthropic vault. From the haku-cloud-kube-token Secret's token-exp key, via the CR's spec.vars."
}
