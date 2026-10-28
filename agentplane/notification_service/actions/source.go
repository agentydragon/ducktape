// Package actions is the Notification service's Actions source: an Action request's events, read from the Action
// Service into the inboxes of the Sessions that follow them.
package actions

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const (
	// eventsPageSize is the most events one read asks for, which is the most the Action Service returns.
	eventsPageSize   = 128
	maxEventSequence = int64(math.MaxInt32)
)

// Source is the worker's Provider for Actions subscriptions.
type Source struct {
	Service action_servicepb.ActionServiceClient
	Store   *store.Store
}

var _ worker.Provider = (*Source)(nil)

// Events returns the next page of the Action request's events after afterSequence, if owner submitted the
// request, and otherwise fails with a *worker.ProviderError of kind access denied. The Action Service lets the
// service read every account's requests, which must not become authority to follow another account's.
//
// The subscription servicer calls it when a subscription is created and shows an access denied failure as
// PERMISSION_DENIED: authorization precedes persistence even when replay starts after the last event.
func (s *Source) Events(ctx context.Context, owner model.ServiceAccount, requestID uuid.UUID, afterSequence int64) ([]storagecodec.ActionEvent, error) {
	request, err := s.Service.GetActionRequest(ctx, &action_servicepb.GetActionRequestRequest{RequestId: requestID.String()})
	if err != nil {
		return nil, rpcFailure(ctx, err)
	}
	if request == nil || request.GetActionRequest() == nil {
		return nil, validationFailure()
	}
	caller, err := serviceAccount(request.GetActionRequest().GetCaller())
	if err != nil {
		return nil, err
	}
	if caller != owner {
		return nil, &worker.ProviderError{Kind: store.FailureAccessDenied, Message: "SourceNotOwnedError"}
	}
	if afterSequence < 0 || afterSequence > maxEventSequence {
		return nil, validationFailure()
	}
	limit := int32(eventsPageSize)
	page, err := s.Service.ListActionRequestEvents(ctx, &action_servicepb.ListActionRequestEventsRequest{
		RequestId: requestID.String(), AfterSequence: int32(afterSequence), Limit: proto.Int32(limit),
	})
	if err != nil {
		return nil, rpcFailure(ctx, err)
	}
	if page == nil {
		return nil, validationFailure()
	}
	events := make([]storagecodec.ActionEvent, len(page.GetEvents()))
	for i, event := range page.GetEvents() {
		if events[i], err = actionEvent(event); err != nil {
			return nil, err
		}
	}
	return events, nil
}

// Reconcile reads the next page of events of the subscription's Action request, and records the ones it has not
// recorded yet, so a long log takes several reads.
func (s *Source) Reconcile(ctx context.Context, claim store.Claim, due store.DueSubscription) error {
	request, ok := due.Source.(model.ActionsSource)
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

func serviceAccount(account *sandboxespb.ServiceAccount) (model.ServiceAccount, error) {
	if account == nil || account.GetNamespace() == "" || account.GetName() == "" {
		return model.ServiceAccount{}, validationFailure()
	}
	return model.ServiceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}, nil
}

func actionEvent(event *action_servicepb.ActionEvent) (storagecodec.ActionEvent, error) {
	if event == nil || event.GetSequence() < 1 || event.GetAt() == nil || event.GetAt().CheckValid() != nil {
		return storagecodec.ActionEvent{}, validationFailure()
	}
	state := event.GetState()
	if !validActionState(state) {
		return storagecodec.ActionEvent{}, validationFailure()
	}
	actor := event.GetActor()
	if err := validateActionActor(actor); err != nil {
		return storagecodec.ActionEvent{}, validationFailure()
	}
	return storagecodec.ActionEvent{
		Sequence: int64(event.GetSequence()), State: state, At: event.GetAt().AsTime().UTC().Truncate(time.Microsecond), Actor: actor,
	}, nil
}

func rpcFailure(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if bearer.IsTokenFileError(err) {
		return err
	}
	code := status.Code(err)
	kind := store.FailureProcessingError
	message := code.String()
	switch code {
	case codes.ResourceExhausted:
		kind = store.FailureRateLimited
	case codes.Unauthenticated, codes.PermissionDenied, codes.NotFound:
		kind = store.FailureAccessDenied
	case codes.Canceled, codes.DeadlineExceeded, codes.Internal, codes.Unknown, codes.Unavailable:
		kind, message = store.FailureUnavailable, "TransportError"
	}
	return &worker.ProviderError{Kind: kind, Message: message}
}

func validationFailure() error {
	return &worker.ProviderError{Kind: store.FailureProcessingError, Message: "ValidationError"}
}

func validActionState(state action_servicepb.ActionState) bool {
	_, known := action_servicepb.ActionState_name[int32(state)]
	return known && state != action_servicepb.ActionState_ACTION_STATE_UNSPECIFIED
}

func validateActionActor(actor *action_servicepb.Principal) error {
	if actor == nil {
		return nil
	}
	if caller := actor.GetCaller(); caller != nil && caller.GetNamespace() != "" && caller.GetName() != "" {
		return nil
	}
	if operator := actor.GetOperator(); operator != nil && operator.GetIssuer() != "" && operator.GetSubject() != "" {
		return nil
	}
	return fmt.Errorf("an Action actor needs a caller account or an operator issuer and subject")
}
