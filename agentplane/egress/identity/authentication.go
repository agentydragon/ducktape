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

type Authenticated struct {
	Principal auth.Principal
	Mode      listener.Mode
	Bearer    string
}

func (Authenticated) String() string   { return "authenticated{redacted}" }
func (Authenticated) GoString() string { return "authenticated{redacted}" }

// Authenticator is shared by the forward listener and the Rules API. Its policy
// and live identity sources are bound once at startup.
//
// CLEANUP(added 2026-11-09): Drop the forward listener's KubernetesBearer mode and its hop
// token verifier once no non-Sandbox workload (Action command/build boxes, Haku UI/Anki) projects a proxy-hop bearer and the proxy's `forward.auth` lists only `spiffe`.
type Authenticator struct {
	policy  listener.Config
	tokens  *Verifier
	records *resolver.Resolver
}

func NewAuthenticator(policy listener.Config, tokens *Verifier, records *resolver.Resolver) (*Authenticator, error) {
	if err := policy.Validate(); err != nil {
		return nil, err
	}
	if tokens == nil || policy.Has(listener.KubernetesBearer) && tokens.hop == nil {
		return nil, errors.New("configured authentication requires its token verifier")
	}
	if policy.Has(listener.SPIFFE) && records == nil {
		return nil, errors.New("SPIFFE authentication requires a live WorkloadCredential resolver")
	}
	return &Authenticator{policy: policy, tokens: tokens, records: records}, nil
}

// Authenticate uses the actual TLS peer and bearer credential. Presence is
// separate from parsing so a malformed credential cannot enable another method.
func (a *Authenticator) Authenticate(ctx context.Context, state *tls.ConnectionState, bearer []string, hasBearer bool) (Authenticated, error) {
	if a.policy.TLS != nil && state == nil {
		return Authenticated{}, &Rejection{Reason: decision.TokenRejected, Detail: "TLS is required"}
	}
	hasCertificate := state != nil && len(state.PeerCertificates) > 0
	mode, err := a.policy.Select(hasCertificate, hasBearer)
	if err != nil {
		reason := decision.TokenRejected
		if !hasCertificate && !hasBearer {
			reason = decision.TokenMissing
		}
		return Authenticated{}, &Rejection{Reason: reason, Detail: "credentials do not select one enabled authentication mode"}
	}
	if mode == listener.SPIFFE {
		principal, err := ResolveSVID(ctx, state, a.records)
		return Authenticated{Principal: principal, Mode: mode}, err
	}
	token, ok := BearerToken(bearer)
	if !ok {
		return Authenticated{}, &Rejection{Reason: decision.TokenMissing, Detail: "invalid bearer credential"}
	}
	principal, err := a.tokens.Identify(ctx, token)
	return Authenticated{Principal: principal, Mode: mode, Bearer: token}, err
}

// Resolve adapts a native gRPC call to the same authentication policy used by
// the forward listener. TLS identity comes only from gRPC's authenticated peer
// credentials; callers cannot supply an identity header.
func (a *Authenticator) Resolve(ctx context.Context) (auth.Principal, error) {
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
	bearer := md.Get("authorization")
	caller, err := a.Authenticate(ctx, state, bearer, len(bearer) > 0)
	if err == nil {
		return caller.Principal, nil
	}
	if ctx.Err() != nil {
		return auth.Principal{}, status.FromContextError(ctx.Err()).Err()
	}
	var rejection *Rejection
	if !errors.As(err, &rejection) {
		return auth.Principal{}, status.Error(codes.Internal, "workload authentication failed")
	}
	if rejection.Reason == decision.Unavailable {
		return auth.Principal{}, status.Error(codes.Unavailable, "workload authentication unavailable")
	}
	return auth.Principal{}, status.Error(codes.Unauthenticated, "workload authentication refused")
}

// VerifyProjected keeps the presented destination tokens bound to the caller's Pod. A standing
// workload's SVID proves no Pod, so it has nothing to bind a token to: every token it presents is
// dropped unreviewed, and a rule needing one refuses with `credential-unavailable`.
func (a *Authenticator) VerifyProjected(ctx context.Context, caller Authenticated, presented map[string]string) map[string]string {
	if caller.Mode == listener.SPIFFE && caller.Principal.PodUID == "" {
		if len(presented) > 0 {
			slog.Warn("dropping projected tokens a standing workload presented: its credential proves no Pod")
		}
		return map[string]string{}
	}
	if caller.Mode == listener.SPIFFE {
		return a.tokens.VerifyProjectedForSandbox(ctx, presented, caller.Principal.Account, caller.Principal.PodUID)
	}
	return a.tokens.VerifyProjected(ctx, presented, caller.Principal)
}
