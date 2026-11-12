package management

import (
	"fmt"
	"strings"
)

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

// IdentityChangeRunningError means the controller has not reported a stopped workload.
type IdentityChangeRunningError struct{ Name string }

func (e *IdentityChangeRunningError) Error() string {
	return fmt.Sprintf("sandbox %s: spec.identity changes only while nothing runs", e.Name)
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

// LauncherPolicySetError identifies policy sets owned by another Sandbox's launcher.
type LauncherPolicySetError struct{ Names []string }

func (e *LauncherPolicySetError) Error() string {
	return fmt.Sprintf("PolicySets %s are other Sandboxes' launcher grants", strings.Join(e.Names, ", "))
}

// SandboxNotFoundError means the requested name is absent or now belongs to another UID.
type SandboxNotFoundError struct{ Name string }

func (e *SandboxNotFoundError) Error() string {
	return fmt.Sprintf("no Agentplane sandbox name='%s'", e.Name)
}
