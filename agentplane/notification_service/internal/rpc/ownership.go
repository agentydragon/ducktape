package rpc

import (
	"context"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// workload is an admitted workload caller as an inbox owner.
type workload struct {
	owner inboxstore.Owner
}

// currentWorkload is the Identity named by the authenticated workload credential.
func (s *Notifications) currentWorkload(ctx context.Context) (workload, error) {
	caller, err := s.admittedActor(ctx)
	if err != nil {
		return workload{}, err
	}
	return workload{owner: inboxstore.Owner{Identity: caller.identity}}, nil
}

// requireSessionIdentity authorizes the caller against the Session authority's current execution Identity.
func (s *Notifications) requireSessionIdentity(ctx context.Context, caller model.Identity, sessionID string) error {
	response, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: sessionID})
	if status.Code(err) == codes.NotFound {
		return inboxstore.ErrNotFound
	}
	if err != nil {
		return unavailable("Session", err)
	}
	recorded, ok := sessionidentity.RecordedIdentity(response.GetSession(), sessionID)
	if !ok || recorded != caller {
		return inboxstore.ErrNotFound
	}
	return nil
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
