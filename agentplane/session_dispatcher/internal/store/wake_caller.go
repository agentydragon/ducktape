package store

import (
	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
)

// WakeCaller is whom a row's attribution lets the lifecycle manager assess a wake for, nil for an
// unattributed row.
func WakeCaller(attribution Attribution) lifecycle.WakeCaller {
	switch attribution := attribution.(type) {
	case *ServiceAttribution:
		if attribution.Action != nil {
			// The Action actor needs its broker grant; keep the effective Identity as attribution.
			return lifecycle.ServiceWakeCaller{SPIFFEID: attribution.ActorSPIFFEID}
		}
		if attribution.Identity != nil {
			return lifecycle.IdentityWakeCaller{Identity: attribution.Identity}
		}
		return lifecycle.ServiceWakeCaller{SPIFFEID: attribution.ActorSPIFFEID}
	case *IdentityAttribution:
		return lifecycle.IdentityWakeCaller{Identity: attribution.Identity}
	default:
		return nil
	}
}
