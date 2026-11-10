// Package lifecycle owns policy and guarded desired-state changes for existing Sandboxes.
package lifecycle

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"strconv"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

// Target identifies one durable Sandbox. Namespace, name and UID are all required because names
// can be reused after deletion; its Pod/VM incarnations are separate controller-managed details.
type Target = identitypb.EnvironmentTarget

// ServiceAccountIdentity pins the effective caller to its live Kubernetes account incarnation.
type ServiceAccountIdentity = identitypb.ServiceAccountIdentity

// WakeCaller is the retained effective caller a wake is assessed for: an AccountWakeCaller or an
// IdentityWakeCaller.
type WakeCaller interface{ isWakeCaller() }

// AccountWakeCaller is a ServiceAccount incarnation.
type AccountWakeCaller struct{ Account *ServiceAccountIdentity }

// IdentityWakeCaller is an Identity incarnation, whether its own workload or a component acting for
// it. It wakes only a Sandbox acting as it, under that Sandbox's resume policy, as an
// AccountWakeCaller wakes the Sandbox running as it.
type IdentityWakeCaller struct{ Identity *identitypb.Identity }

func (AccountWakeCaller) isWakeCaller()  {}
func (IdentityWakeCaller) isWakeCaller() {}

// WakeBudgetKey is whose budget a wake attempt spends: the namespace, name and UID of the caller
// incarnation. An Identity's namespace is the one the manager reads Identities in.
type WakeBudgetKey struct{ Namespace, Name, UID string }

// WakeBudget reserves a bounded wake attempt without depending on Dispatcher storage.
// Implementations share reservations across replicas and explicit and automatic wake paths.
type WakeBudget interface {
	ReserveWakeAttempt(context.Context, WakeBudgetKey, string, int64) (time.Duration, error)
}

// NewResumeToken creates the opaque precondition returned by Discovery for a particular target
// UID and wake generation. It is deterministic and is not a capability; the lifecycle manager
// still checks current identity, policy and suspension state before it mutates the target.
func NewResumeToken(target *Target, generation int64) string {
	if target == nil || target.Namespace == "" || target.Name == "" || target.Uid == "" || generation < 0 {
		return ""
	}
	value := target.Namespace + "\x00" + target.Name + "\x00" + target.Uid + "\x00" + strconv.FormatInt(generation, 10)
	sum := sha256.Sum256([]byte("agentplane.environment-resume.v1\x00" + value))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}
