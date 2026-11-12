// Package callers defines the ServiceAccount label contract for Action Service callers.
package callers

const (
	// ServiceAccountLabelKey marks a ServiceAccount whose workloads may call the Action Service.
	// It grants no Action; the account's PolicyBindings still decide which Actions are allowed.
	ServiceAccountLabelKey = "agentplane.allegedly.works/use-action-service"
	// ServiceAccountLabelValue is the value that admits the caller label.
	ServiceAccountLabelValue = "true"
)
