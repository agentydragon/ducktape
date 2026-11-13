package dispatcher

import (
	"context"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/internal/placement"
)

func testPlacement(spec *sessionpb.SessionSpec, sandboxName, sandboxUID string) placement.SessionPlacement {
	if placement.PlacementOf(spec) == "" {
		spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}
	}
	return placement.PlacementOf(spec)
}

func (s *Store) CreateSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account) (*Session, error) {
	return s.openSession(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec, sandboxName, sandboxUID), spec, setupScript, wakeCaller,
		SessionExecution{Identity: testExecutionIdentity()}, nil, nil)
}

func testExecutionIdentity() *identitypb.Identity {
	return &identitypb.Identity{Name: "dispatcher-test-identity", Uid: "00000000-0000-4000-8000-00000000a000"}
}

func (s *Store) CreateSessionForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account, executionIdentity *identitypb.Identity, caller ServiceAccountCaller) (*Session, error) {
	testPlacement(spec, sandboxName, sandboxUID)
	return s.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID, spec, setupScript, wakeCaller, SessionExecution{Identity: executionIdentity}, caller)
}
