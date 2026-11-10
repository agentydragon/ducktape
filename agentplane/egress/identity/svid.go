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
// which a forwarded projected token must come from. A standing record, with no owner and no binding,
// proves no Pod: its principal has none, and no projected token can be bound to it.
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
	// CLEANUP(added 2026-11-10): an Identity subject keys the decision in EGRESS_IDENTITY_RULES
	// (agentplane/plans/task_dag.md); until then the proxy has no rules for one.
	account, ok := resolved.Subject.(resolver.ServiceAccount)
	if !ok {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "the proxy does not yet decide for an Identity subject"}
	}
	principal := auth.Principal{Account: auth.Account{Namespace: account.Namespace, Name: account.Name}}
	if resolved.Binding == nil {
		return principal, nil
	}
	// A bound incarnation authenticates only once its owner has named the Pod it runs in.
	if resolved.Pod == nil {
		return auth.Principal{}, &Rejection{Reason: decision.TokenRejected, Detail: "WorkloadCredential has no accepted Pod"}
	}
	principal.PodName, principal.PodUID = resolved.Pod.Name, string(resolved.Pod.UID)
	return principal, nil
}
