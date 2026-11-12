package api

import "k8s.io/apimachinery/pkg/util/validation"

// MaxSandboxNameLength is the maximum length of a Sandbox name.
const MaxSandboxNameLength = 57

// IsValidSandboxName reports whether name meets the Sandbox DNS label contract.
func IsValidSandboxName(name string) bool {
	return len(name) > 0 && len(name) <= MaxSandboxNameLength && len(validation.IsDNS1123Label(name)) == 0
}
