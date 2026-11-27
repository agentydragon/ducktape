package wake

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
)

// Each wake caller principal spends its own budget: an Identity by its Sandbox namespace, name and
// UID, a service by its SPIFFE ID.
func TestBudgetKeyNamesTheCallerPrincipal(t *testing.T) {
	manager := &Manager{namespace: "test-wake-namespace"}
	for _, tc := range []struct {
		caller lifecycle.WakeCaller
		want   WakeBudgetKey
	}{
		{
			caller: lifecycle.IdentityWakeCaller{Identity: &identitypb.Identity{Name: "test-identity", Uid: "00000000-0000-4000-8000-0000000000b1"}},
			want:   WakeBudgetKey{Namespace: "test-wake-namespace", Name: "test-identity", UID: "00000000-0000-4000-8000-0000000000b1"},
		},
		{
			caller: lifecycle.ServiceWakeCaller{SPIFFEID: "spiffe://test.invalid/test-service"},
			want:   WakeBudgetKey{ServiceSPIFFEID: "spiffe://test.invalid/test-service"},
		},
	} {
		if got := manager.budgetKey(tc.caller); got != tc.want {
			t.Errorf("budgetKey(%T) = %+v, want %+v", tc.caller, got, tc.want)
		}
	}
}
