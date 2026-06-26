variable "haku_kube_token" {
  type        = string
  ephemeral   = true
  sensitive   = true
  description = "haku-k8s Authentik JWT for the static_bearer vault credential. Injected into the tofu-controller runner from the haku-cloud-kube-token Secret as TF_VAR_haku_kube_token. ephemeral so it never lands in plan or state (the credential's token attribute is also write-only). See main.tf rotation note."
}

variable "haku_kube_token_wo_version" {
  type        = number
  description = "The haku JWT's exp epoch (seconds). Drives the write-only token_wo_version so each rotation (new token -> later exp) re-sends the token into the Anthropic vault. From the haku-cloud-kube-token Secret's token-exp key (TF_VAR_haku_kube_token_wo_version)."
}
