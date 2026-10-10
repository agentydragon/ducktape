terraform {
  required_version = ">= 1.0"

  required_providers {
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 3.2.0" }
    routeros   = { source = "terraform-routeros/routeros", version = "~> 1.99.0" }
  }
}
