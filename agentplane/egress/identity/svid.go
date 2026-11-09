package identity

import (
	"context"
	"crypto/tls"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
)

// ResolveSVID rechecks the live WorkloadCredential the peer certificate names on every request. Only
// the record supplies the ServiceAccount used as the policy subject, and the Pod its owner accepted,
// which a forwarded projected token must come from.
func ResolveSVID(ctx context.Context, state *tls.ConnectionState, records *resolver.Resolver) (auth.Principal, error) {
	if state == nil || len(state.PeerCertificates) == 0 {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "invalid SVID peer certificate"}
	}
	if records == nil {
		return auth.Principal{}, &Rejection{Reason: decision.Unavailable, Detail: "SVID authentication is not configured"}
	}
	resolved, err := records.Resolve(ctx, state.PeerCertificates[0])
	if errors.Is(err, resolver.ErrUnavailable) {
		return auth.Principal{}, &Rejection{Reason: decision.Unavailable, Detail: "live WorkloadCredential lookup failed"}
	}
	if err != nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "SVID does not name a current WorkloadCredential"}
	}
	if resolved.Pod == nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "WorkloadCredential has no accepted Pod"}
	}
	return auth.Principal{
		Account: auth.Account{Namespace: resolved.Subject.Namespace, Name: resolved.Subject.Name},
		PodName: resolved.Pod.Name, PodUID: string(resolved.Pod.UID),
	}, nil
}
