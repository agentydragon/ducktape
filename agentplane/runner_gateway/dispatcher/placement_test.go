package dispatcher

import (
	"context"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func testPlacement(spec *sessionpb.SessionSpec, sandboxName, sandboxUID string) SessionPlacement {
	if placementOf(spec) == "" {
		spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{
			Environment: &sessionpb.ExactEnvironment{Name: sandboxName, Uid: sandboxUID}, Harness: sessionpb.Harness_HARNESS_CODEX,
		}}}
	}
	return placementOf(spec)
}

func (s *Store) CreateSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account) (*Session, error) {
	return s.openSession(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec, sandboxName, sandboxUID), spec, setupScript, wakeCaller, SessionExecution{}, nil)
}

func (s *Store) CreateSessionForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account, executionPrincipal *ServiceAccountIdentity, caller CallerIdentity) (*Session, error) {
	return s.CreateSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec, sandboxName, sandboxUID), spec, setupScript, wakeCaller, SessionExecution{Principal: executionPrincipal}, caller)
}
