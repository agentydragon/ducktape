package api

import (
	"context"
	"log/slog"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
)

func (s *Notifications) owner(ctx context.Context, account model.ServiceAccount, sandboxUID string) (store.Owner, error) {
	principal, err := auth.Caller(ctx)
	if err != nil {
		return store.Owner{}, admissionRefused{err}
	}
	owner := store.Owner{Account: account, AccountUID: principal.ServiceAccountUID, SandboxUID: sandboxUID}
	if owner.AccountUID == "" {
		return store.Owner{}, store.ErrNotFound
	}
	inboxes, err := s.store.UnestablishedInboxes(ctx, owner)
	if err != nil {
		return store.Owner{}, err
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
			return store.Owner{}, err
		}
	}
	return owner, nil
}
