package inventory

import "fmt"

// SandboxNotFoundError means the requested name is absent or now belongs to another UID.
type SandboxNotFoundError struct{ Name string }

func (e *SandboxNotFoundError) Error() string {
	return fmt.Sprintf("no Agentplane sandbox name='%s'", e.Name)
}
