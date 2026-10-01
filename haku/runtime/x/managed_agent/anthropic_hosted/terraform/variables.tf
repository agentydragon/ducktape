# No input variables: the provider's API key comes from the ANTHROPIC_API_KEY
# env var (injected into the tofu-controller runner), and the static_bearer
# historical credential's token was read from haku-cloud-kube-token via a
# kubernetes_secret_v1 data source (see main.tf). Present to
# satisfy tflint's terraform_standard_module_structure rule.
