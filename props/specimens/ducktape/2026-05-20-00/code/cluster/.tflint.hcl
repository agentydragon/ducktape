rule "terraform_deprecated_index" {
  enabled = true
}

rule "terraform_deprecated_interpolation" {
  enabled = true
}

rule "terraform_documented_outputs" {
  enabled = true
}

rule "terraform_documented_variables" {
  enabled = true
}

rule "terraform_naming_convention" {
  enabled = true
  format  = "snake_case"
}

rule "terraform_required_providers" {
  enabled = false  # Modules inherit provider versions from root - source is sufficient
}

rule "terraform_required_version" {
  enabled = false  # Only root configuration needs this - modules inherit from root
}

rule "terraform_standard_module_structure" {
  enabled = false  # Root configuration, not a reusable module
}

rule "terraform_typed_variables" {
  enabled = true
}

rule "terraform_unused_declarations" {
  enabled = true
}

rule "terraform_unused_required_providers" {
  enabled = false  # Root module provides providers for child modules to inherit
}

# Custom rule to prevent terraform blocks with version constraints in non-root files
rule "terraform_module_version" {
  enabled = true
}
