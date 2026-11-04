package dispatcher

import (
	"context"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func testPlacement(spec *sessionpb.SessionSpec) SessionPlacement {
	if spec.GetHarness() == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
		return CentralSampler
	}
	return EnvironmentHosted
}

func (s *Store) OpenSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account) (*Opening, error) {
	return s.openSession(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec), spec, setupScript, wakeCaller, nil, nil)
}

func (s *Store) OpenSessionForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account, executionPrincipal *ServiceAccountIdentity, caller CallerIdentity) (*Opening, error) {
	return s.OpenSessionWithPlacementForCaller(ctx, sessionID, sandboxName, sandboxUID, testPlacement(spec), spec, setupScript, wakeCaller, executionPrincipal, caller)
}
