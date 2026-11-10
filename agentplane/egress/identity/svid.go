package identity

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
)

// ResolveSVID rechecks the live WorkloadCredential the peer certificate names on every request. Only
// the record supplies the policy subject -- a ServiceAccount, or an Identity read live with its UID
// -- and the Pod its owner accepted, which a forwarded projected token must come from. A standing
// record, with no owner and no binding, proves no Pod: its subject has none, and no projected token
// can be bound to it.
func ResolveSVID(ctx context.Context, state *tls.ConnectionState, records *resolver.Resolver) (auth.Subject, error) {
	if state == nil || len(state.PeerCertificates) == 0 {
		return nil, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID peer certificate"}
	}
	if records == nil {
		return nil, &Rejection{Reason: decision.Unavailable, Detail: "SVID authentication is not configured"}
	}
	resolved, err := records.Resolve(ctx, state.PeerCertificates[0])
	if errors.Is(err, resolver.ErrUnavailable) {
		return nil, &Rejection{Reason: decision.Unavailable, Detail: "live WorkloadCredential lookup failed"}
	}
	if err != nil {
		return nil, &Rejection{Reason: decision.TokenRejected, Detail: "SVID does not name a current WorkloadCredential"}
	}
	var podName, podUID string
	if resolved.Binding != nil {
		// A bound incarnation authenticates only once its owner has named the Pod it runs in.
		if resolved.Pod == nil {
			return nil, &Rejection{Reason: decision.TokenRejected, Detail: "WorkloadCredential has no accepted Pod"}
		}
		podName, podUID = resolved.Pod.Name, string(resolved.Pod.UID)
	}
	switch subject := resolved.Subject.(type) {
	case resolver.ServiceAccount:
		return auth.Principal{
			Account: auth.Account{Namespace: subject.Namespace, Name: subject.Name}, PodName: podName, PodUID: podUID,
		}, nil
	case resolver.Identity:
		return auth.IdentityPrincipal{
			Identity: auth.Identity{Name: subject.Name, UID: string(subject.UID)}, Workload: auth.WorkloadOf(resolved),
			PodName: podName, PodUID: podUID,
		}, nil
	default:
		panic(fmt.Sprintf("unknown subject %T", resolved.Subject))
	}
}
