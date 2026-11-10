package identity

import (
	"context"
	"crypto/tls"
	"errors"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
	"github.com/agentydragon/ducktape/agentplane/workload_identity/resolver"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/peer"
	"google.golang.org/grpc/status"
)

// Authenticated is the subject a caller's SVID proved: a ServiceAccount or an Identity.
type Authenticated struct {
	Subject auth.Subject
}

// Authenticator is shared by the forward listener and the Rules API, both of which authenticate
// workload SVIDs alone. Its policy and live identity sources are bound once at startup.
type Authenticator struct {
	policy  listener.Config
	tokens  *Verifier
	records *resolver.Resolver
}

func NewAuthenticator(policy listener.Config, tokens *Verifier, records *resolver.Resolver) (*Authenticator, error) {
	if err := policy.Validate(); err != nil {
		return nil, err
	}
	if !policy.Has(listener.SPIFFE) || policy.Has(listener.KubernetesBearer) {
		return nil, errors.New("workload authentication accepts SPIFFE alone")
	}
	if tokens == nil || records == nil {
		return nil, errors.New("SPIFFE authentication requires a token verifier and a live WorkloadCredential resolver")
	}
	return &Authenticator{policy: policy, tokens: tokens, records: records}, nil
}

// Authenticate uses the actual TLS peer. A bearer beside it is refused rather than ignored, so a
// caller never stands on a credential this listener does not read.
func (a *Authenticator) Authenticate(ctx context.Context, state *tls.ConnectionState, hasBearer bool) (Authenticated, error) {
	if a.policy.TLS != nil && state == nil {
		return Authenticated{}, &Rejection{Reason: decision.TokenRejected, Detail: "TLS is required"}
	}
	hasCertificate := state != nil && len(state.PeerCertificates) > 0
	if _, err := a.policy.Select(hasCertificate, hasBearer); err != nil {
		reason := decision.TokenRejected
		if !hasCertificate && !hasBearer {
			reason = decision.TokenMissing
		}
		return Authenticated{}, &Rejection{Reason: reason, Detail: "credentials do not select one enabled authentication mode"}
	}
	subject, err := ResolveSVID(ctx, state, a.records)
	return Authenticated{Subject: subject}, err
}

// ResolveSubject adapts a native gRPC call to the same authentication policy used by
// the forward listener. TLS identity comes only from gRPC's authenticated peer
// credentials; callers cannot supply an identity header.
func (a *Authenticator) ResolveSubject(ctx context.Context) (auth.Subject, error) {
	var state *tls.ConnectionState
	if remote, ok := peer.FromContext(ctx); ok {
		switch info := remote.AuthInfo.(type) {
		case credentials.TLSInfo:
			state = &info.State
		case *credentials.TLSInfo:
			if info != nil {
				state = &info.State
			}
		}
	}
	md, _ := metadata.FromIncomingContext(ctx)
	caller, err := a.Authenticate(ctx, state, len(md.Get("authorization")) > 0)
	if err == nil {
		return caller.Subject, nil
	}
	if ctx.Err() != nil {
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	var rejection *Rejection
	if !errors.As(err, &rejection) {
		return nil, status.Error(codes.Internal, "workload authentication failed")
	}
	if rejection.Reason == decision.Unavailable {
		return nil, status.Error(codes.Unavailable, "workload authentication unavailable")
	}
	return nil, status.Error(codes.Unauthenticated, "workload authentication refused")
}

// VerifyProjected keeps the presented destination tokens bound to the caller's Pod. A standing
// workload's SVID proves no Pod, so it has nothing to bind a token to: every token it presents is
// dropped unreviewed, and a rule needing one refuses with `credential-unavailable`. An Identity's
// Pod is provenance, not its subject: a Pod token proves a ServiceAccount, never an Identity, so an
// Identity caller's tokens are dropped unreviewed too.
func (a *Authenticator) VerifyProjected(ctx context.Context, caller Authenticated, presented map[string]string) map[string]string {
	principal, ok := caller.Subject.(auth.Principal)
	if !ok {
		if len(presented) > 0 {
			slog.Warn("dropping projected tokens an Identity presented: a Pod token cannot prove one")
		}
		return map[string]string{}
	}
	if principal.PodUID == "" {
		if len(presented) > 0 {
			slog.Warn("dropping projected tokens a standing workload presented: its credential proves no Pod")
		}
		return map[string]string{}
	}
	return a.tokens.VerifyProjectedForSandbox(ctx, presented, principal.Account, principal.PodUID)
}
