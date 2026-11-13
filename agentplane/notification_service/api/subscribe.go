package api

import (
	"context"
	"errors"
	"fmt"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// subscribe creates the owner's subscription once the Session and the source are known to be the owner's to
// follow. The source is authorized before anything is persisted, even when replay starts after the last event.
//
// An Identity owns only Sessions the Session authority recorded as executing as that exact Identity
// incarnation in the Sandbox controlling its credential. ServiceAccounts remain authenticated actors and
// message senders, but Session ownership is Identity-only.
func (s *Notifications) subscribe(ctx context.Context, caller actor, body model.Subscribe) (store.Subscription, error) {
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
	var owner store.Owner
	switch caller := caller.(type) {
	case accountActor:
		return store.Subscription{}, store.ErrNotFound
	case identityActor:
		recorded, ok := sessionidentity.IdentityRun(session.GetSession(), body.SessionID, body.DestinationRef)
		if !ok || recorded != caller.identity || caller.sandbox != body.DestinationRef {
			return store.Subscription{}, store.ErrNotFound
		}
		owner = store.IdentityOwner{Identity: caller.identity, SandboxUID: body.DestinationRef.UID}
	default:
		panic(fmt.Sprintf("unknown actor %T", caller))
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
		if _, err := s.actions.Events(ctx, owner, source.RequestID, source.AfterSequence); err != nil {
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
	identityOwner, ok := owner.(store.IdentityOwner)
	if !ok {
		return store.Subscription{}, store.ErrNotFound
	}
	subscription, err := s.store.SubscribeAsIdentity(ctx, identityOwner.Identity, body, access)
	if err != nil {
		return store.Subscription{}, err
	}
	return subscription, nil
}

// destination resolves the Sandbox the reference names to the exact incarnation, which must name the Identity
// owner in `spec.identity`, whose incarnation the caller's credential or the Session authority fenced. A
// Sandbox outside the service's namespace, a missing or replaced one, and another owner's are all not found.
func (s *Notifications) destination(ctx context.Context, owner store.Owner, ref model.DestinationRef) (*sandboxreader.Identity, error) {
	view, err := s.sandbox(ctx, ref)
	if err != nil {
		return nil, err
	}
	runsAs, err := s.sandboxRunsAs(ctx, view, owner)
	if err != nil {
		return nil, err
	}
	if !runsAs {
		return nil, errSandboxMissing
	}
	return view.Identity, nil
}

// sandboxRunsAs reads the live execution Identity for the exact Sandbox incarnation and compares
// both UID and name. A reused Identity name cannot make a retained Session appear owned by its successor.
func (s *Notifications) sandboxRunsAs(ctx context.Context, view sandboxreader.View, owner store.Owner) (bool, error) {
	switch owner := owner.(type) {
	case store.IdentityOwner:
		identity, found, err := s.sandboxes.ExecutionIdentity(ctx, view.Identity)
		if err != nil {
			return false, unavailable("sandbox execution Identity", err)
		}
		return found && identity != nil && identity.Name == owner.Identity.Name && identity.Uid == owner.Identity.UID, nil
	default:
		return false, nil
	}
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
