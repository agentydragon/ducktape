// Package identity authenticates workloads using the configured SVID or Kubernetes
// bearer policy and verifies destination-audience tokens offered for substitution.
package identity

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

// Rejection is a credential that does not prove a workload identity, or an identity
// that could not be checked; Reason is what the client sees.
type Rejection struct {
	Reason decision.Reason
	Detail string
}

func (r *Rejection) Error() string { return fmt.Sprintf("%s: %s", r.Reason, r.Detail) }

// Verifier resolves hop and projected bearers with the shared TokenReview resolver.
// Its bounded acceptance cache follows workload_auth/auth.Resolver.
type Verifier struct {
	hop *auth.Resolver
	// projected has one resolver per audience this proxy will substitute a sidecar's projected token
	// for. An audience with none is one no operator configured, so a credential naming it resolves
	// to nothing however well-formed the token presented for it was.
	projected map[string]*auth.Resolver
}

// NewVerifier returns a Verifier for hop bearers and, by audience, projected tokens.
func NewVerifier(hop *auth.Resolver, projected map[string]*auth.Resolver) *Verifier {
	return &Verifier{hop: hop, projected: projected}
}

// BearerToken reads the token out of a `Proxy-Authorization` header, which must be present exactly
// once: a request carrying two credentials has not said which it stands on, and picking either
// would let a caller smuggle a second past whatever inspected the first.
func BearerToken(values []string) (string, bool) {
	token, err := auth.BearerToken(values)
	return token, err == nil
}

// Identify reviews a hop bearer. A failure is a *Rejection: TokenRejected when the token does not
// prove a workload this proxy serves, Unavailable when the review could not be answered.
func (v *Verifier) Identify(ctx context.Context, token string) (auth.Principal, error) {
	principal, err := v.hop.ResolveToken(ctx, token)
	if err != nil {
		return auth.Principal{}, rejection(err)
	}
	return principal, nil
}

func rejection(err error) *Rejection {
	if errors.Is(err, auth.ErrInvalidBearer) {
		return &Rejection{Reason: decision.TokenRejected, Detail: err.Error()}
	}
	return &Rejection{Reason: decision.Unavailable, Detail: err.Error()}
}

// ProjectedTokenHeader carries the audience-scoped tokens the sidecar offers for substitution, one
// `<audience> <token>` per value. The hop bearer is not one of them: it authenticates the hop, where
// these are only candidates to substitute, and are worth nothing until reviewed.
const ProjectedTokenHeader = "X-Agentplane-Workload-Token"

// ParseProjectedTokens reads the values of ProjectedTokenHeader into tokens by audience. A value
// that is not an `<audience> <token>` pair is dropped, without logging it: it is a bearer for some
// audience even when its pair cannot be read.
func ParseProjectedTokens(values []string) map[string]string {
	presented := map[string]string{}
	for _, value := range values {
		audience, token, found := strings.Cut(value, " ")
		if found && audience != "" && token != "" {
			presented[audience] = token
		} else {
			slog.Warn("ignoring a malformed projected token entry", "header", ProjectedTokenHeader)
		}
	}
	return presented
}

// VerifyProjected returns the presented tokens that prove the same Pod as principal, by audience.
// The review is what makes the substituted value the caller's own: the API server answers for the
// Pod the token is bound to, and only a token bound to the very Pod that authenticated this hop is
// kept. Without it the proxy would spend whatever token it was handed on a destination the
// presenter cannot reach directly, which is the confused deputy this exists to refuse.
//
// A token that does not verify is dropped rather than refused: it denies the rule that names it
// with `credential-unavailable` at the point of use, and leaves traffic that never wanted it alone.
func (v *Verifier) VerifyProjected(ctx context.Context, presented map[string]string, principal auth.Principal) map[string]string {
	verified := map[string]string{}
	for audience, token := range presented {
		resolver, configured := v.projected[audience]
		if !configured {
			slog.Warn("no resolver configured for projected token audience", "audience", audience)
			continue
		}
		reviewed, err := resolver.ResolveToken(ctx, token)
		if err != nil {
			slog.Warn("projected token rejected", "audience", audience, "error", err)
			continue
		}
		if reviewed != principal {
			slog.Warn("projected token is not this hop's Pod", "audience", audience)
			continue
		}
		verified[audience] = token
	}
	return verified
}

// VerifyProjectedForSandbox keeps only audience-scoped tokens whose TokenReview
// names the ServiceAccount and Pod incarnation bound to a live Sandbox. Unlike
// the legacy token hop, the SVID caller does not carry a ServiceAccount UID, so
// the comparison is deliberately limited to the account and Pod UID that the
// proxy resolved from Sandbox status.
func (v *Verifier) VerifyProjectedForSandbox(
	ctx context.Context, presented map[string]string, account auth.Account, podUID string,
) map[string]string {
	verified := map[string]string{}
	for audience, token := range presented {
		resolver, configured := v.projected[audience]
		if !configured {
			slog.Warn("no resolver configured for projected token audience", "audience", audience)
			continue
		}
		reviewed, err := resolver.ResolveToken(ctx, token)
		if err != nil {
			slog.Warn("projected token rejected", "audience", audience, "error", err)
			continue
		}
		if reviewed.Account != account || reviewed.PodUID != podUID {
			slog.Warn("projected token is not bound to the SVID Sandbox account and Pod", "audience", audience)
			continue
		}
		verified[audience] = token
	}
	return verified
}
