# No input variables: the claude-managed-agents provider reads its API key from
# the ANTHROPIC_API_KEY env var (injected into the tofu-controller runner from
# the haku-cloud-anthropic-api-key Secret), and the static_bearer credential's
# token is seeded out-of-band (see main.tf). Present to satisfy tflint's
# terraform_standard_module_structure rule.
