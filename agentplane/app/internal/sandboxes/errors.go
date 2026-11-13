package sandboxes

import "fmt"

// SandboxQuiescingError is the app-facing error for a Sandbox that cannot resume while quiescing.
type SandboxQuiescingError struct{ Name string }

func (e *SandboxQuiescingError) Error() string {
	return fmt.Sprintf("sandbox %s is quiescing; retry resume after it is suspended", e.Name)
}
