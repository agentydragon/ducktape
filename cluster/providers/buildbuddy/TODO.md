# BuildBuddy provider TODO

- Provision BuildBuddy API keys for in-cluster consumers using the Crossplane
  `APIKey` resource. Give each consumer only the capabilities it needs and wire
  its connection Secret to the workload. Identify existing credential owners
  and plan their handoff, including the provider's admin credential bootstrap.
- Retire the dormant `litellm_keys` Terraform state safely: its remaining
  `terraform_data.buildbuddy_live_tests_secret` had a destroy provisioner that
  deletes the Crossplane-managed BuildBuddy secret. Forget it without restoring
  or running that provisioner.
