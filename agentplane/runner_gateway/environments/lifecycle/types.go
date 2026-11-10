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

// Caller is the effective caller a lifecycle decision is made for: a ServiceAccountCaller or an
// IdentityCaller.
type Caller interface{ isCaller() }

// ServiceAccountCaller is a direct or delegated ServiceAccount caller, pinned to its account UID.
type ServiceAccountCaller struct{ Account *ServiceAccountIdentity }

// IdentityCaller is a direct caller whose workload credential names an Identity incarnation.
type IdentityCaller struct{ Identity *identitypb.Identity }

func (ServiceAccountCaller) isCaller() {}
func (IdentityCaller) isCaller()       {}

// WakeBudget reserves a bounded wake attempt without depending on Dispatcher storage.
// Implementations share reservations across replicas and explicit and automatic wake paths.
type WakeBudget interface {
	ReserveWakeAttempt(context.Context, Caller, string, int64) (time.Duration, error)
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
