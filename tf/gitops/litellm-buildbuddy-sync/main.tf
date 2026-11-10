terraform {
  required_version = ">= 1.0"

  required_providers {
    external = {
      source  = "hashicorp/external"
      version = "~> 2.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
  }
}

removed {
  from = terraform_data.buildbuddy_live_tests_secret

  # Forget the writer without running its destroy provisioner. Keep this
  # tombstone until the remaining BuildBuddy Terraform machinery is retired.
  lifecycle {
    destroy = false
  }
}
