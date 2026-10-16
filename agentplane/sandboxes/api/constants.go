package api

import "fmt"

const (
	SandboxKind  = "Sandbox"
	SandboxLabel = "agentplane.allegedly.works/sandbox"

	ConditionRunning         = "Running"
	ConditionWorkloadReady   = "WorkloadReady"
	ConditionRunnerConnected = "RunnerConnected"
	ConditionBootstrapped    = "Bootstrapped"
	ConditionReady           = "Ready"
	ConditionQuiesced        = "Quiesced"

	ControlServiceSuffix   = "-control"
	ControlServicePortName = "grpc"
	ControlServicePort     = 8080
)

// ControlServiceTargetTemplate is the default target used by both the controller and dispatcher.
func ControlServiceTargetTemplate() string {
	return fmt.Sprintf("{sandbox}%s.{namespace}.svc:%d", ControlServiceSuffix, ControlServicePort)
}
