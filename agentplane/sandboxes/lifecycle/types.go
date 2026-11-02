// Package lifecycle owns policy and guarded desired-state changes for existing Sandboxes.
package lifecycle

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"strconv"
	"time"
)

// Target identifies one durable Sandbox. Namespace, name and UID are all required because names
// can be reused after deletion; its Pod/VM incarnations are separate controller-managed details.
type Target struct {
	Namespace string
	Name      string
	UID       string
}

// ServiceAccount identifies the effective caller by its live Kubernetes identity.
// The UID distinguishes a replacement account with the same namespace and name.
type ServiceAccount struct {
	Namespace string
	Name      string
	UID       string
}

// WakeBudget reserves a bounded wake attempt without depending on Dispatcher storage.
// Implementations share reservations across replicas and explicit and automatic wake paths.
type WakeBudget interface {
	ReserveWakeAttempt(context.Context, ServiceAccount, string, int64) (time.Duration, error)
}

// NewResumeToken creates the opaque precondition returned by Discovery for a particular target
// UID and wake generation. It is deterministic and is not a capability; the lifecycle service
// still checks current identity, policy and suspension state before it mutates the target.
func NewResumeToken(target Target, generation int64) string {
	if target.Namespace == "" || target.Name == "" || target.UID == "" || generation < 0 {
		return ""
	}
	value := target.Namespace + "\x00" + target.Name + "\x00" + target.UID + "\x00" + strconv.FormatInt(generation, 10)
	sum := sha256.Sum256([]byte("agentplane.environment-resume.v1\x00" + value))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}
