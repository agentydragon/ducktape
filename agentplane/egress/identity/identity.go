// Package identity verifies audience-scoped tokens a sidecar offers for credential substitution.
// The proxy caller identity is established by the workload SVID on the TLS connection.
package identity

import (
	"context"
	"fmt"
	"log/slog"
	"regexp"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

// Rejection is a workload identity that could not be authenticated; Reason is what the client sees.
type Rejection struct {
	Reason decision.Reason
	Detail string
}

func (r *Rejection) Error() string { return fmt.Sprintf("%s: %s", r.Reason, r.Detail) }

// Verifier reviews destination tokens with the API server on every call.
type Verifier struct {
	// projected has one resolver per audience this proxy will substitute a sidecar's projected token
	// for. An audience with none is one no operator configured, so a credential naming it resolves
	// to nothing however well-formed the token presented for it was.
	projected map[string]*auth.Resolver
}

// NewVerifier returns a Verifier for destination-scoped projected tokens.
func NewVerifier(projected map[string]*auth.Resolver) *Verifier {
	return &Verifier{projected: projected}
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

// ProjectedTokenHeader carries the audience-scoped tokens the sidecar offers for substitution, one
// `<audience> <token>` per value. These tokens are independent of the SVID that authenticates the hop.
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

// VerifyProjectedForSandbox keeps only audience-scoped tokens whose TokenReview
// names the ServiceAccount and Pod incarnation bound to a live Sandbox. The
// caller account and Pod UID come from the live Sandbox status associated with
// its verified SVID.
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
