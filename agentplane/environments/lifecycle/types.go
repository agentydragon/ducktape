// Package lifecycle shares policy and guarded Sandbox writes across environment lifecycle owners.
package lifecycle

import (
	"crypto/sha256"
	"encoding/base64"
	"strconv"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

// Target identifies one durable Sandbox. Namespace, name and UID are all required because names
// can be reused after deletion; its Pod/VM incarnations are separate controller-managed details.
type Target = identitypb.EnvironmentTarget

// ServiceAccountIdentity pins the effective caller to its live Kubernetes account incarnation.
type ServiceAccountIdentity = identitypb.ServiceAccountIdentity

// WakeCaller is the caller a wake is assessed for: a ServiceAccount (never authorized), a
// first-party service broker, or an Identity acting for its bound Sandbox.
type WakeCaller interface{ isWakeCaller() }

// AccountWakeCaller is a ServiceAccount incarnation. It holds no wake or resume authority;
// same-name workload self-wake is retired.
type AccountWakeCaller struct{ Account *ServiceAccountIdentity }

// IdentityWakeCaller is an Identity incarnation, whether its own workload or a component acting for
// it. It wakes only a Sandbox acting as it, under that Sandbox's existing resume policy opt-in.
type IdentityWakeCaller struct{ Identity *identitypb.Identity }

// ServiceWakeCaller is a verified first-party service. Its SPIFFE ID stays distinct from
// Kubernetes ServiceAccount and Identity provenance throughout wake authorization and budgeting.
type ServiceWakeCaller struct{ SPIFFEID string }

func (AccountWakeCaller) isWakeCaller()  {}
func (IdentityWakeCaller) isWakeCaller() {}
func (ServiceWakeCaller) isWakeCaller()  {}

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
