package policyaccess

import "fmt"

// BindingNotFoundError means the namespaced PolicyBinding does not exist.
type BindingNotFoundError struct{ Name string }

func (e *BindingNotFoundError) Error() string {
	return fmt.Sprintf("no PolicyBinding name='%s'", e.Name)
}

// FluxOwnedBindingError means Flux is the source of truth for this PolicyBinding.
type FluxOwnedBindingError struct{ Name string }

func (e *FluxOwnedBindingError) Error() string {
	return fmt.Sprintf("PolicyBinding name='%s' comes from git; remove it there", e.Name)
}

// SandboxOwnedBindingError means the Sandbox controller keeps this binding equal to its spec.
type SandboxOwnedBindingError struct{ Name string }

func (e *SandboxOwnedBindingError) Error() string {
	return fmt.Sprintf("PolicyBinding name='%s' is the Sandbox's own; change the Sandbox's policy sets instead", e.Name)
}

// CreatedBindingNotVisibleError means the API accepted a grant but its binding was not in the
// subsequent namespace read. Retrying the grant could create a second authority, so callers must
// reconcile before trying again.
type CreatedBindingNotVisibleError struct{ Name string }

func (e *CreatedBindingNotVisibleError) Error() string {
	return "Created PolicyBinding is no longer visible; reconcile before retrying"
}
