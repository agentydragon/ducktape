// Package identity authenticates the workload behind a proxied connection: the hop bearer in
// `Proxy-Authorization` and the audience-scoped tokens a sidecar offers for substitution. A refusal
// carries the reason the client sees.
package identity

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

// Rejection is a bearer that does not prove a workload identity, or an identity that could not be
// checked; Reason is what the client sees.
type Rejection struct {
	Reason decision.Reason
	Detail string
}

func (r *Rejection) Error() string { return fmt.Sprintf("%s: %s", r.Reason, r.Detail) }

// Verifier reviews bearers with the API server on every call: a revoked token stops working here at
// the moment it stops working anywhere.
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

// bearer matches `Bearer <token>`, the token drawn from the RFC 6750 `b64token` characters, which a
// projected ServiceAccount token (a JWT) is made of.
var bearer = regexp.MustCompile(`(?i)\ABearer +([A-Za-z0-9._~+/=-]+)\z`)

// BearerToken reads the token out of a `Proxy-Authorization` header, which must be present exactly
// once: a request carrying two credentials has not said which it stands on, and picking either
// would let a caller smuggle a second past whatever inspected the first.
func BearerToken(values []string) (string, bool) {
	if len(values) != 1 {
		return "", false
	}
	match := bearer.FindStringSubmatch(values[0])
	if match == nil {
		return "", false
	}
	return match[1], true
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
