package store

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
)

func testPlacement(spec *sessionpb.SessionSpec, sandboxName, sandboxUID string) placement.SessionPlacement {
	if placement.PlacementOf(spec) == "" {
		spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}
	}
	return placement.PlacementOf(spec)
}

func createSession(s *Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string) (*Session, error) {
	return createSessionForCaller(s, ctx, sessionID, sandboxName, sandboxUID, spec, setupScript, testExecutionIdentity(), testStoreCaller())
}

func testExecutionIdentity() *identitypb.Identity {
	return &identitypb.Identity{Name: "dispatcher-test-identity", Uid: "00000000-0000-4000-8000-00000000a000"}
}

// testStoreCaller is the privileged component caller store fixtures write as.
func testStoreCaller() ServiceCaller { return ServiceCaller{SPIFFEID: peertls.AppURI} }

func createSessionForCaller(s *Store, ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, executionIdentity *identitypb.Identity, caller Caller) (*Session, error) {
	return s.CreateSessionWithAdmissionCheck(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec, sandboxName, sandboxUID),
		spec, setupScript, SessionExecution{Identity: executionIdentity}, caller, nil)
}
