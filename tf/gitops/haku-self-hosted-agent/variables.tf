# No input variables: the provider's API key comes from the ANTHROPIC_API_KEY
# env var (injected into the tofu-controller runner), and the static_bearer
# credentials' tokens are read in-cluster from the tana-mcp + gmail-labeling
# Secrets via kubernetes_secret_v1 data sources (see main.tf). Present to
# satisfy tflint's terraform_standard_module_structure rule.
