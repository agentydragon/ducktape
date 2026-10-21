// Package actions is the Notification service's Actions source: an Action request's events, read from the
// Action Service into the inboxes of the Sessions that follow them.
package actions

import (
	"context"
	"errors"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/google/uuid"
)

// Source is the worker's Provider for Actions subscriptions.
type Source struct {
	Service ActionService
	Store   *store.Store
}

var _ worker.Provider = (*Source)(nil)

// Events returns the next page of the Action request's events after afterSequence, if owner submitted the
// request, and otherwise fails with a *worker.ProviderError of kind access denied. The Action Service lets the
// service read every account's requests, which must not become authority to follow another account's.
//
// Stage H's servicer calls it when a subscription is created, as the Python service does, and shows an access
// denied failure as PERMISSION_DENIED: authorization precedes persistence even when replay starts after the
// last event.
func (s *Source) Events(ctx context.Context, owner store.ServiceAccount, requestID uuid.UUID, afterSequence int64) ([]store.ActionEvent, error) {
	caller, err := s.Service.Caller(ctx, requestID)
	if err != nil {
		return nil, err
	}
	if caller != owner {
		return nil, &worker.ProviderError{Kind: store.FailureAccessDenied, Message: "SourceNotOwnedError"}
	}
	return s.Service.Events(ctx, requestID, afterSequence)
}

// Reconcile reads the next page of events of the subscription's Action request, and records the ones it has not
// recorded yet. The Python service reads a page of 128 each time and reads again five seconds later, so a long
// log takes several reads.
func (s *Source) Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	request, ok := due.Source.(store.ActionsSource)
	if !ok || due.ActionsAfterSequence == nil {
		return fmt.Errorf("subscription %s is not an Actions subscription", due.ID)
	}
	events, err := s.Events(ctx, claim.Owner, request.RequestID, *due.ActionsAfterSequence)
	if err != nil {
		return err
	}
	err = s.Store.RecordActionEvents(ctx, claim, due, events)
	if errors.Is(err, store.ErrActionHistoryGap) {
		return &worker.ProviderError{Kind: store.FailureProcessingError, Message: "ConflictError"}
	}
	return err
}
