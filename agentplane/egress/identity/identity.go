// Package identity authenticates workload SVIDs and verifies destination-audience tokens offered
// for substitution.
package identity

import (
	"context"
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

// Verifier resolves projected bearers with the shared TokenReview resolver.
// Its bounded acceptance cache follows workload_auth/auth.Resolver.
type Verifier struct {
	// projected has one resolver per audience this proxy will substitute a sidecar's projected token
	// for. An audience with none is one no operator configured, so a credential naming it resolves
	// to nothing however well-formed the token presented for it was.
	projected map[string]*auth.Resolver
}

// NewVerifier returns a Verifier for projected tokens, by audience.
func NewVerifier(projected map[string]*auth.Resolver) *Verifier {
	return &Verifier{projected: projected}
}

// ProjectedTokenHeader carries the audience-scoped tokens the sidecar offers for substitution, one
// `<audience> <token>` per value. They do not authenticate the hop; they are only candidates to
// substitute, and are worth nothing until reviewed.
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
// names the ServiceAccount and Pod incarnation bound to a live Sandbox. The SVID
// caller does not carry a ServiceAccount UID, so the comparison is deliberately
// limited to the account and Pod UID that the proxy resolved from its record.
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
