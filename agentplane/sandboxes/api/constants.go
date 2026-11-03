package api

import "fmt"

const (
	SandboxKind  = "Sandbox"
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// CreateIntentAnnotation is the receipt a Sandbox's creator writes with it, so that its retry of the same create
	// adopts the Sandbox where another create of the name is refused. Each creator compares it only with its own
	// receipts.
	CreateIntentAnnotation = "agentplane.allegedly.works/create-intent"

	ConditionRunning                     = "Running"
	ConditionWorkloadReady               = "WorkloadReady"
	ConditionEnvironmentRuntimeConnected = "EnvironmentRuntimeConnected"
	ConditionBootstrapped                = "Bootstrapped"
	ConditionReady                       = "Ready"
	ConditionQuiesced                    = "Quiesced"

	RunnerProxySandboxMetadataKey               = "x-agentplane-sandbox"
	RunnerProxySandboxNamespaceMetadataKey      = "x-agentplane-sandbox-namespace"
	RunnerProxySandboxUIDMetadataKey            = "x-agentplane-sandbox-uid"
	RunnerProxySandboxOwnerNamespaceMetadataKey = "x-agentplane-sandbox-owner-namespace"
	RunnerProxySandboxOwnerNameMetadataKey      = "x-agentplane-sandbox-owner-name"

	ControlServiceSuffix   = "-control"
	ControlServicePortName = "grpc"
	ControlServicePort     = 8080
	// EnvironmentRuntimeServicePort is the runner's mTLS listener port in a Pod or KubeVirt guest.
	EnvironmentRuntimeServicePort     = 9090
	EnvironmentRuntimeCredentialLabel = "agentplane.allegedly.works/environment-runtime-credential"
)

// ControlServiceTargetTemplate is the default target used by both the controller and dispatcher.
func ControlServiceTargetTemplate() string {
	return fmt.Sprintf("{sandbox}%s.{namespace}.svc:%d", ControlServiceSuffix, ControlServicePort)
}
