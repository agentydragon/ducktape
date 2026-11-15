package api

import "fmt"

const (
	SandboxKind  = "Sandbox"
	SandboxLabel = "agentplane.allegedly.works/sandbox"
	// SandboxUIDLabel names the Sandbox incarnation associated with a workload credential's Certificate
	// and Secret, so they remain findable after the Sandbox and its name are gone.
	SandboxUIDLabel = "agentplane.allegedly.works/sandbox-uid"
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
	RunnerProxyIncarnationUIDMetadataKey        = "x-agentplane-incarnation-uid"

	ControlServicePort   = 8080
	RunnerGatewayService = "runner-gateway"
	// EnvironmentRuntimeServicePort is the Environment Runtime's mTLS listener port in a Pod or KubeVirt guest.
	EnvironmentRuntimeServicePort           = 9090
	EnvironmentRuntimeCredentialMetadataKey = "agentplane.allegedly.works/environment-runtime-credential"
)

// RunnerGatewayTargetTemplate routes each Sandbox's Environment Runtime RPCs through the stable Runner Gateway Service.
func RunnerGatewayTargetTemplate() string {
	return fmt.Sprintf("%s.{namespace}.svc:%d", RunnerGatewayService, ControlServicePort)
}
