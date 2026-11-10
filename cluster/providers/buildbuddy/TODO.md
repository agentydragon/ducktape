# BuildBuddy provider TODO

- Provision BuildBuddy API keys for in-cluster consumers using the Crossplane
  `APIKey` resource. Give each consumer only the capabilities it needs and wire
  its connection Secret to the workload. Identify existing credential owners
  and plan their handoff, including the provider's admin credential bootstrap.
