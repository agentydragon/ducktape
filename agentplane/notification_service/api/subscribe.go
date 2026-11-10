package api

import (
	"context"
	"errors"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// subscribe creates the owner's subscription once the Session and the source are known to be the owner's to
// follow. The source is authorized before anything is persisted, even when replay starts after the last event.
func (s *Notifications) subscribe(ctx context.Context, owner model.ServiceAccount, body model.Subscribe) (store.Subscription, error) {
	session, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: body.SessionID})
	if status.Code(err) == codes.NotFound {
		return store.Subscription{}, errNoSession
	}
	if err != nil {
		return store.Subscription{}, unavailable("Session", err)
	}
	target := sessionidentity.Target(session.GetSession(), body.SessionID)
	if target == nil {
		return store.Subscription{}, unavailable("Session", errors.New("incomplete Session placement"))
	}
	body.DestinationRef = model.DestinationRef{Namespace: s.namespace, Name: target.GetName(), UID: target.GetUid()}
	ownerUID, err := sessionidentity.AccountUID(session.GetSession(), body.SessionID, body.DestinationRef, owner)
	if err != nil {
		return store.Subscription{}, err
	}
	principal, err := auth.Caller(ctx)
	if err != nil {
		return store.Subscription{}, admissionRefused{err}
	}
	if principal.ServiceAccountUID != ownerUID {
		return store.Subscription{}, store.ErrNotFound
	}
	if err := s.store.EstablishInboxOwner(ctx, owner, body.DestinationRef, body.SessionID, ownerUID); err != nil {
		return store.Subscription{}, err
	}
	if err := body.Validate(); err != nil {
		return store.Subscription{}, err
	}
	sandbox, err := s.destination(ctx, owner, body.DestinationRef)
	if err != nil {
		return store.Subscription{}, err
	}
	sessions, err := s.runner.ListSessions(ctx, sandbox)
	if err != nil {
		return store.Subscription{}, unavailable("runner", err)
	}
	if !slices.Contains(sessions, body.SessionID) {
		return store.Subscription{}, errNoSession
	}
	var access *store.GitHubAccessKey
	switch source := body.Source.(type) {
	case model.ActionsSource:
		if _, err := s.actions.Events(ctx, owner, source.RequestID, source.AfterSequence, ownerUID); err != nil {
			return store.Subscription{}, err
		}
	case model.GitHubSource:
		if s.github == nil {
			return store.Subscription{}, errGitHubDisabled
		}
		authorized, err := s.github.Authorize(ctx, source)
		if err != nil {
			return store.Subscription{}, err
		}
		access = &authorized
	default:
		return store.Subscription{}, errors.New("a validated source is an Action request or a GitHub subject")
	}
	subscription, err := s.store.Subscribe(ctx, owner, body, access, ownerUID)
	if err != nil {
		return store.Subscription{}, err
	}
	return subscription, s.establishIdentity(ctx, session.GetSession(), body.DestinationRef, body.SessionID)
}

// destination resolves the Sandbox the reference names to the exact incarnation, which must run as the owner. A
// Sandbox outside the service's namespace, a missing or replaced one, and another account's are all not found.
func (s *Notifications) destination(ctx context.Context, owner model.ServiceAccount, ref model.DestinationRef) (*sandboxreader.Identity, error) {
	view, err := s.sandbox(ctx, ref)
	if err != nil {
		return nil, err
	}
	// A Sandbox runs as the ServiceAccount of its own name.
	if (model.ServiceAccount{Namespace: view.Identity.Namespace, Name: view.Identity.Name}) != owner {
		return nil, errSandboxMissing
	}
	return view.Identity, nil
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
