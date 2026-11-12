package api

import (
	"context"
	"fmt"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
)

// workload is an admitted workload caller as an inbox owner, with the live Sandbox incarnation it
// runs in.
type workload struct {
	owner   store.Owner
	sandbox model.DestinationRef
}

// currentWorkload is the caller's owner for its current Sandbox incarnation. A ServiceAccount caller
// runs in the Sandbox of its own name; an Identity caller in the Sandbox controlling its workload
// credential. Either fails with errSandboxMissing when that Sandbox is gone.
func (s *Notifications) currentWorkload(ctx context.Context) (workload, error) {
	caller, err := s.admittedActor(ctx)
	if err != nil {
		return workload{}, err
	}
	switch caller := caller.(type) {
	case identityActor:
		if _, err := s.sandbox(ctx, caller.sandbox); err != nil {
			return workload{}, err
		}
		inboxOwner, err := s.identityOwner(ctx, caller.identity, caller.sandbox)
		return workload{owner: inboxOwner, sandbox: caller.sandbox}, err
	case accountActor:
		view, err := s.sandbox(ctx, model.DestinationRef{Namespace: caller.account.Namespace, Name: caller.account.Name})
		if err != nil {
			return workload{}, err
		}
		inboxOwner, err := s.owner(ctx, caller.account, view.Identity.Uid)
		return workload{owner: inboxOwner, sandbox: model.DestinationRef{Namespace: caller.account.Namespace, Name: caller.account.Name, UID: view.Identity.Uid}}, err
	default:
		panic(fmt.Sprintf("unknown actor %T", caller))
	}
}

func (s *Notifications) owner(ctx context.Context, account model.ServiceAccount, sandboxUID string) (store.Owner, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, admissionRefused{err}
	}
	owner := store.AccountOwner{Account: account, AccountUID: principal.ServiceAccountUID, SandboxUID: sandboxUID}
	if owner.AccountUID == "" {
		return nil, store.ErrNotFound
	}
	inboxes, err := s.store.UnestablishedInboxes(ctx, owner)
	if err != nil {
		return nil, err
	}
	for _, inbox := range inboxes {
		response, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: inbox.SessionID})
		if err != nil {
			// Reconciliation is best effort: already established inboxes remain accessible
			// while the authority is unavailable. Unknown identities stay inaccessible.
			slog.WarnContext(ctx, "inbox owner identity unavailable", "inbox", inbox.ID, "error", err)
			continue
		}
		uid, err := sessionidentity.AccountUID(response.GetSession(), inbox.SessionID, inbox.DestinationRef, account)
		if err != nil {
			continue
		}
		if err := s.store.EstablishInboxOwner(ctx, account, inbox.DestinationRef, inbox.SessionID, uid); err != nil {
			return nil, err
		}
		if err := s.establishIdentity(ctx, response.GetSession(), inbox.DestinationRef, inbox.SessionID); err != nil {
			return nil, err
		}
	}
	return owner, nil
}

// identityOwner is owner for an Identity caller: it establishes, from the Session authority, the
// Identity of its Sandbox's inboxes no owner has been established for yet.
func (s *Notifications) identityOwner(ctx context.Context, identity model.Identity, sandbox model.DestinationRef) (store.Owner, error) {
	owner := store.IdentityOwner{Identity: identity, SandboxUID: sandbox.UID}
	inboxes, err := s.store.UnestablishedInboxes(ctx, owner)
	if err != nil {
		return nil, err
	}
	for _, inbox := range inboxes {
		response, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: inbox.SessionID})
		if err != nil {
			slog.WarnContext(ctx, "inbox owner identity unavailable", "inbox", inbox.ID, "error", err)
			continue
		}
		if err := s.establishIdentity(ctx, response.GetSession(), inbox.DestinationRef, inbox.SessionID); err != nil {
			return nil, err
		}
	}
	return owner, nil
}

// establishIdentity records the Identity the Session authority recorded the inbox's Session as
// executing as, if any, beside the ServiceAccount owner.
func (s *Notifications) establishIdentity(ctx context.Context, session *dispatcherpb.Session, ref model.DestinationRef, sessionID string) error {
	identity, ok := sessionidentity.Identity(session, sessionID, ref)
	if !ok {
		return nil
	}
	return s.store.EstablishInboxIdentity(ctx, ref, sessionID, identity)
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
