package rpc

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// workload is an admitted workload caller as an inbox owner, with the live Sandbox incarnation it
// runs in.
type workload struct {
	owner   inboxstore.Owner
	sandbox model.DestinationRef
}

// currentWorkload is the exact Identity owner for the Sandbox controlling its credential.
// A missing Sandbox fails with errSandboxMissing; ServiceAccounts are not inbox owners.
func (s *Notifications) currentWorkload(ctx context.Context) (workload, error) {
	caller, err := s.admittedActor(ctx)
	if err != nil {
		return workload{}, err
	}
	view, err := s.sandbox(ctx, caller.sandbox)
	if err != nil {
		return workload{}, err
	}
	owner := inboxstore.Owner{Identity: caller.identity, SandboxUID: caller.sandbox.UID}
	runsAs, err := s.sandboxRunsAs(ctx, view, owner)
	if err != nil {
		return workload{}, err
	}
	if !runsAs {
		return workload{}, errSandboxMissing
	}
	return workload{
		owner:   owner,
		sandbox: caller.sandbox,
	}, nil
}

// operator reports whether the admitted caller is a configured service-SVID diagnostic reader.
func (s *Notifications) operator(ctx context.Context) (bool, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false, admissionRefused{err}
	}
	if _, ok := admission.(auth.IdentityAdmission); ok {
		return false, nil
	}
	if service, ok := admission.(auth.ServiceAdmission); ok {
		_, allowed := s.operatorServices[service.Caller.SPIFFEID]
		return allowed, nil
	}
	return false, admissionRefused{fmt.Errorf("unexpected admission %T", admission)}
}
