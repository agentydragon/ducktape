package store

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

const fakeWakeServiceSPIFFEID = "spiffe://fake-wake-service.invalid/runner"

func TestWakeCallerMapsActionAttributionToServiceAndPreservesOtherKinds(t *testing.T) {
	identity := &identitypb.Identity{Name: "fake-wake-identity", Uid: "fake-identity-incarnation"}
	service := &ServiceAttribution{ActorSPIFFEID: fakeWakeServiceSPIFFEID}
	delegated := &ServiceAttribution{
		ActorSPIFFEID: peertls.ActionsURI,
		Identity:      identity,
		Action:        &ActionCorrelation{RequestID: "fake-action-request", ExecutionID: "fake-action-execution"},
	}

	if got, ok := WakeCaller(delegated).(lifecycle.ServiceWakeCaller); !ok || got.SPIFFEID != peertls.ActionsURI {
		t.Fatalf("Action-attributed wake caller = %#v, want Actions service caller", WakeCaller(delegated))
	}
	if delegated.Identity != identity || delegated.Action == nil {
		t.Fatalf("Action attribution was changed: %#v", delegated)
	}

	cases := []struct {
		name        string
		attribution Attribution
		check       func(lifecycle.WakeCaller) bool
	}{
		{
			name:        "direct service",
			attribution: service,
			check: func(caller lifecycle.WakeCaller) bool {
				got, ok := caller.(lifecycle.ServiceWakeCaller)
				return ok && got.SPIFFEID == fakeWakeServiceSPIFFEID
			},
		},
		{
			name:        "service delegation without Action proof",
			attribution: &ServiceAttribution{ActorSPIFFEID: fakeWakeServiceSPIFFEID, Identity: identity},
			check: func(caller lifecycle.WakeCaller) bool {
				got, ok := caller.(lifecycle.IdentityWakeCaller)
				return ok && got.Identity == identity
			},
		},
		{
			name:        "Identity workload",
			attribution: &IdentityAttribution{Identity: identity},
			check: func(caller lifecycle.WakeCaller) bool {
				got, ok := caller.(lifecycle.IdentityWakeCaller)
				return ok && got.Identity == identity
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := WakeCaller(tc.attribution); !tc.check(got) {
				t.Fatalf("wake caller = %#v, attribution = %#v", got, tc.attribution)
			}
		})
	}
}
