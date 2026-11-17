package rpc

import (
	"context"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sourceadapter"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sessionidentity"

	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// subscribe creates the owner's subscription once the Session and the source are known to be the owner's to
// follow. The source is authorized before anything is persisted, even when replay starts after the last event.
//
// An Identity owns only Sessions the Session authority recorded as executing as that exact Identity
// incarnation.
func (s *Notifications) subscribe(ctx context.Context, caller identityActor, body sourceadapter.Subscribe) (inboxstore.Subscription, error) {
	if err := body.Validate(); err != nil {
		return inboxstore.Subscription{}, err
	}
	session, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: body.SessionID})
	if status.Code(err) == codes.NotFound {
		return inboxstore.Subscription{}, errNoSession
	}
	if err != nil {
		return inboxstore.Subscription{}, unavailable("Session", err)
	}
	recorded, ok := sessionidentity.RecordedIdentity(session.GetSession(), body.SessionID)
	if !ok || recorded != caller.identity {
		return inboxstore.Subscription{}, inboxstore.ErrNotFound
	}
	owner := inboxstore.Owner{Identity: caller.identity}
	var authorization sourceadapter.Authorization
	switch source := body.Source.(type) {
	case sourceadapter.ActionsSource:
		authorization = sourceadapter.ActionsAuthorization{}
		if _, err := s.actions.Events(ctx, owner, source.RequestID, source.AfterSequence); err != nil {
			return inboxstore.Subscription{}, err
		}
	case sourceadapter.GitHubSource:
		if s.github == nil {
			return inboxstore.Subscription{}, errGitHubDisabled
		}
		authorized, err := s.github.Authorize(ctx, source.Source)
		if err != nil {
			return inboxstore.Subscription{}, err
		}
		authorization = sourceadapter.GitHubAuthorization{Access: authorized}
	default:
		return inboxstore.Subscription{}, errors.New("a validated source is an Action request or a GitHub subject")
	}
	return s.store.Subscribe(ctx, owner, body, authorization)
}

// sandboxRunsAs resolves the live execution Identity for the exact Sandbox incarnation and compares
// both name and UID. A same-name replacement cannot inherit a retained Session's authority.
func (s *Notifications) sandboxRunsAs(ctx context.Context, view sandboxreader.View, owner inboxstore.Owner) (bool, error) {
	identity, found, err := s.sandboxes.ExecutionIdentity(ctx, view.Identity)
	if err != nil {
		return false, unavailable("sandbox execution Identity", err)
	}
	return found && identity != nil && owner.Identity.Name != "" && owner.Identity.UID != "" &&
		identity.Name == owner.Identity.Name && identity.Uid == owner.Identity.UID, nil
}

// sandbox reads the Sandbox the reference names, which must be in the service's namespace and, when the reference
// gives a UID, that incarnation; else it is not found.
func (s *Notifications) sandbox(ctx context.Context, ref model.DestinationRef) (sandboxreader.View, error) {
	if ref.Namespace != s.namespace {
		return sandboxreader.View{}, errSandboxMissing
	}
	view, err := s.sandboxes.Get(ctx, ref.Name)
	switch {
	case status.Code(err) == codes.NotFound:
		return sandboxreader.View{}, errSandboxMissing
	case err != nil:
		return sandboxreader.View{}, unavailable("sandbox lookup", err)
	case ref.UID != "" && view.Identity.Uid != ref.UID:
		return sandboxreader.View{}, errSandboxMissing
	}
	return view, nil
}
