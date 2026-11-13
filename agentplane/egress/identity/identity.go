// Package identity authenticates workload SVIDs against live WorkloadCredentials.
package identity

import (
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
)

// UntrustedWorkloadTokenHeader is a retired token-forwarding header. Both the sidecar and proxy
// discard it so an untrusted caller cannot smuggle it through to a destination.
const UntrustedWorkloadTokenHeader = "X-Agentplane-Workload-Token"

// Rejection is a credential that does not prove a workload identity, or an identity
// that could not be checked; Reason is what the client sees.
type Rejection struct {
	Reason decision.Reason
	Detail string
}

func (r *Rejection) Error() string { return fmt.Sprintf("%s: %s", r.Reason, r.Detail) }
