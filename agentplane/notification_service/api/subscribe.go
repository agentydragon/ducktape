package api

import (
	"context"
	"errors"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// subscribe creates the owner's subscription once the Session and the source are known to be the owner's to
// follow. The source is authorized before anything is persisted, even when replay starts after the last event.
func (s *Notifications) subscribe(ctx context.Context, owner store.ServiceAccount, body store.Subscribe) (store.Subscription, error) {
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
	switch source := body.Source.(type) {
	case store.ActionsSource:
		if _, err := s.actions.Events(ctx, owner, source.RequestID, source.AfterSequence); err != nil {
			return store.Subscription{}, err
		}
		return s.store.Subscribe(ctx, owner, body, nil)
	case store.GitHubSource:
		if s.github == nil {
			return store.Subscription{}, errGitHubDisabled
		}
		access, err := s.github.Authorize(ctx, source)
		if err != nil {
			return store.Subscription{}, err
		}
		return s.store.Subscribe(ctx, owner, body, &access)
	default:
		return store.Subscription{}, errors.New("a validated source is an Action request or a GitHub subject")
	}
}

// destination resolves the Sandbox the reference names to the exact incarnation, which must run as the owner. A
// Sandbox outside the service's namespace, a missing or replaced one, and another account's are all not found.
func (s *Notifications) destination(ctx context.Context, owner store.ServiceAccount, ref store.DestinationRef) (sandboxreader.Identity, error) {
	view, err := s.sandbox(ctx, ref)
	if err != nil {
		return sandboxreader.Identity{}, err
	}
	// A Sandbox runs as the ServiceAccount of its own name.
	if (store.ServiceAccount{Namespace: view.Identity.Namespace, Name: view.Identity.Name}) != owner {
		return sandboxreader.Identity{}, errSandboxMissing
	}
	return view.Identity, nil
}

// sandbox reads the Sandbox the reference names, which must be in the service's namespace and, when the reference
// gives a UID, that incarnation; else it is not found.
func (s *Notifications) sandbox(ctx context.Context, ref store.DestinationRef) (sandboxreader.View, error) {
	if ref.Namespace != s.namespace {
		return sandboxreader.View{}, errSandboxMissing
	}
	view, err := s.sandboxes.Get(ctx, ref.Name)
	switch {
	case status.Code(err) == codes.NotFound:
		return sandboxreader.View{}, errSandboxMissing
	case err != nil:
		return sandboxreader.View{}, unavailable("sandbox lookup", err)
	case ref.UID != "" && view.Identity.UID != ref.UID:
		return sandboxreader.View{}, errSandboxMissing
	}
	return view, nil
}
