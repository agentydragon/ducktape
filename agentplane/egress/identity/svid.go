package identity

import (
	"context"
	"crypto/tls"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/resolver"
)

// ResolveSVID rechecks the live WorkloadCredential the peer certificate names on every request. Only
// the record supplies the Identity subject and the Pod its owner accepted as provenance. A standing record, with no owner and no binding,
// proves no Pod, so decisions record no source Pod.
func ResolveSVID(ctx context.Context, state *tls.ConnectionState, records *resolver.Resolver) (auth.IdentityPrincipal, error) {
	if state == nil || len(state.PeerCertificates) == 0 {
		return auth.IdentityPrincipal{}, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID peer certificate"}
	}
	if records == nil {
		return auth.IdentityPrincipal{}, &Rejection{Reason: decision.Unavailable, Detail: "SVID authentication is not configured"}
	}
	resolved, err := records.Resolve(ctx, state.PeerCertificates[0])
	if errors.Is(err, resolver.ErrUnavailable) {
		return auth.IdentityPrincipal{}, &Rejection{Reason: decision.Unavailable, Detail: "live WorkloadCredential lookup failed"}
	}
	if err != nil {
		return auth.IdentityPrincipal{}, &Rejection{Reason: decision.TokenRejected, Detail: "SVID does not name a current WorkloadCredential"}
	}
	var podName, podUID string
	if resolved.Binding != nil {
		// A bound incarnation authenticates only once its owner has named the Pod it runs in.
		if resolved.Pod == nil {
			return auth.IdentityPrincipal{}, &Rejection{Reason: decision.TokenRejected, Detail: "WorkloadCredential has no accepted Pod"}
		}
		podName, podUID = resolved.Pod.Name, string(resolved.Pod.UID)
	}
	return auth.IdentityPrincipal{
		Identity: auth.Identity{Name: resolved.Subject.Name, UID: string(resolved.Subject.UID)}, Workload: auth.WorkloadOf(resolved),
		PodName: podName, PodUID: podUID,
	}, nil
}
