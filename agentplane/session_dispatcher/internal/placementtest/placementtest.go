// Package placementtest gives test sessions a placement when their spec names none.
package placementtest

import (
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
)

// EnsurePlacement sets an environment-hosted Codex placement on spec unless it has one, and returns the placement.
func EnsurePlacement(spec *sessionpb.SessionSpec, sandboxName, sandboxUID string) placement.SessionPlacement {
	if placement.PlacementOf(spec) == "" {
		spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}
	}
	return placement.PlacementOf(spec)
}
