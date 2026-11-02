package policy

import (
	"fmt"
	"strings"
)

// UnknownPolicySetError names requested PolicySets absent from this namespace.
type UnknownPolicySetError struct {
	Names []string
	Kind  string
}

func (e *UnknownPolicySetError) Error() string {
	return fmt.Sprintf("no %s in the namespace is named %s", e.Kind, strings.Join(e.Names, ", "))
}
