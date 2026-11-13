package api

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
)

// workload is an admitted workload caller as an inbox owner, with the live Sandbox incarnation it
// runs in.
type workload struct {
	owner   store.Owner
	sandbox model.DestinationRef
}

// currentWorkload is the exact Identity owner for the Sandbox controlling its credential.
// A missing Sandbox fails with errSandboxMissing; ServiceAccounts are not inbox owners.
func (s *Notifications) currentWorkload(ctx context.Context) (workload, error) {
	caller, err := s.admittedActor(ctx)
	if err != nil {
		return workload{}, err
	}
	switch caller := caller.(type) {
	case identityActor:
		view, err := s.sandbox(ctx, caller.sandbox)
		if err != nil {
			return workload{}, err
		}
		owner := store.Owner{Identity: caller.identity, SandboxUID: caller.sandbox.UID}
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
	case accountActor:
		return workload{}, store.ErrNotFound
	default:
		panic(fmt.Sprintf("unknown actor %T", caller))
	}
}

// operator reports whether the admitted caller is the configured operator reader, a ServiceAccount.
func (s *Notifications) operator(ctx context.Context) (bool, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false, admissionRefused{err}
	}
	if _, ok := admission.(auth.IdentityAdmission); ok {
		return false, nil
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return false, err
	}
	return s.operatorReader != "" && caller.Namespace == s.namespace && caller.Name == s.operatorReader, nil
}
