package api

import "fmt"

const (
	SandboxKind  = "Sandbox"
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// CreateIntentAnnotation is the receipt a Sandbox's creator writes with it, so that its retry of the same create
	// adopts the Sandbox where another create of the name is refused. Each creator compares it only with its own
	// receipts. Must equal CREATE_INTENT in agentplane/sandboxes/kubernetes_views.py.
	CreateIntentAnnotation = "agentplane.allegedly.works/create-intent"

	ConditionRunning         = "Running"
	ConditionWorkloadReady   = "WorkloadReady"
	ConditionRunnerConnected = "RunnerConnected"
	ConditionBootstrapped    = "Bootstrapped"
	ConditionReady           = "Ready"
	ConditionQuiesced        = "Quiesced"

	RunnerProxySandboxMetadataKey               = "x-agentplane-sandbox"
	RunnerProxySandboxUIDMetadataKey            = "x-agentplane-sandbox-uid"
	RunnerProxySandboxOwnerNamespaceMetadataKey = "x-agentplane-sandbox-owner-namespace"
	RunnerProxySandboxOwnerNameMetadataKey      = "x-agentplane-sandbox-owner-name"

	ControlServiceSuffix   = "-control"
	ControlServicePortName = "grpc"
	ControlServicePort     = 8080
	// RunnerServicePort is the Pod or KubeVirt guest port used when the SandboxTemplate selects
	// forward transport. The KubeVirt launcher exposes this guest port through masquerade.
	RunnerServicePort     = 9090
	RunnerTransportLabel  = "agentplane.allegedly.works/runner-transport"
	RunnerCredentialLabel = "agentplane.allegedly.works/runner-credential"
)

// ControlServiceTargetTemplate is the default target used by both the controller and dispatcher.
func ControlServiceTargetTemplate() string {
	return fmt.Sprintf("{sandbox}%s.{namespace}.svc:%d", ControlServiceSuffix, ControlServicePort)
}
