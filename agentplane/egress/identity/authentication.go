package identity

import (
	"context"
	"crypto/tls"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/listener"
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
type Authenticator struct {
	policy  listener.Config
	tokens  *Verifier
	readers map[string]*sandboxreader.Reader
}

func NewAuthenticator(policy listener.Config, tokens *Verifier, readers map[string]*sandboxreader.Reader) (*Authenticator, error) {
	if err := policy.Validate(); err != nil {
		return nil, err
	}
	if tokens == nil || policy.Has(listener.KubernetesBearer) && tokens.hop == nil {
		return nil, errors.New("configured authentication requires its token verifier")
	}
	if policy.Has(listener.SPIFFE) && len(readers) == 0 {
		return nil, errors.New("SPIFFE authentication requires live Sandbox readers")
	}
	for namespace, reader := range readers {
		if namespace == "" || reader == nil {
			return nil, errors.New("Sandbox readers require nonempty namespaces and readers")
		}
	}
	return &Authenticator{policy: policy, tokens: tokens, readers: readers}, nil
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
		principal, err := ResolveSVID(ctx, state, a.readers)
		return Authenticated{Principal: principal, Mode: mode}, err
	}
	token, ok := BearerToken(bearer)
	if !ok {
		return Authenticated{}, &Rejection{Reason: decision.TokenMissing, Detail: "invalid bearer credential"}
	}
	principal, err := a.tokens.Identify(ctx, token)
	return Authenticated{Principal: principal, Mode: mode, Bearer: token}, err
}

func (a *Authenticator) VerifyProjected(ctx context.Context, caller Authenticated, presented map[string]string) map[string]string {
	if caller.Mode == listener.SPIFFE {
		return a.tokens.VerifyProjectedForSandbox(ctx, presented, caller.Principal.Account, caller.Principal.PodUID)
	}
	return a.tokens.VerifyProjected(ctx, presented, caller.Principal)
}
