package sandboxes

import "fmt"

// SandboxNotFoundError means the requested name is absent or now belongs to another UID.
type SandboxNotFoundError struct{ Name string }

func (e *SandboxNotFoundError) Error() string {
	return fmt.Sprintf("no Agentplane sandbox name='%s'", e.Name)
}

// SandboxConflictError means a name is already owned by a different Create intent.
type SandboxConflictError struct{ Name string }

func (e *SandboxConflictError) Error() string { return e.Name }

// InvalidSandboxError is a rejected create request.
type InvalidSandboxError struct{ Reason string }

func (e *InvalidSandboxError) Error() string { return e.Reason }

// SandboxRunningError means teardown is refused until the operator suspends the Sandbox.
type SandboxRunningError struct{ Name string }

func (e *SandboxRunningError) Error() string {
	return fmt.Sprintf("sandbox %s is running; suspend it before deleting it", e.Name)
}

// SandboxModeUnknownError means the controller has not reported a usable operating mode.
type SandboxModeUnknownError struct{ Name string }

func (e *SandboxModeUnknownError) Error() string {
	return fmt.Sprintf("sandbox %s has no controller-reported operating mode yet", e.Name)
}

// SandboxTearingDownError means deletion has started and the Sandbox no longer accepts changes.
type SandboxTearingDownError struct{ Name string }

func (e *SandboxTearingDownError) Error() string {
	return fmt.Sprintf("sandbox %s is being deleted", e.Name)
}

// SandboxQuiescingError means the controller is sealing the current incarnation before stopping it.
type SandboxQuiescingError struct{ Name string }

func (e *SandboxQuiescingError) Error() string {
	return fmt.Sprintf("sandbox %s is quiescing; retry resume after it is suspended", e.Name)
}

// UnknownPolicySetError names requested PolicySets absent from this namespace.
type UnknownPolicySetError struct {
	Names []string
	Kind  string
}

func (e *UnknownPolicySetError) Error() string {
	return fmt.Sprintf("no %s in the namespace is named %s", e.Kind, joinNames(e.Names))
}

func joinNames(names []string) string {
	joined := ""
	for index, name := range names {
		if index > 0 {
			joined += ", "
		}
		joined += name
	}
	return joined
}
