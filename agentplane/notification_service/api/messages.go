package api

import (
	"context"
	"errors"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func (s *Notifications) SendMessage(ctx context.Context, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	return serve(ctx, s, request, s.sendMessage)
}

// sendMessage accepts the message once the sender's current grants allow it to the Sandbox the destination names
// now. Revoking a grant later never withdraws it.
func (s *Notifications) sendMessage(ctx context.Context, sender model.ServiceAccount, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	if request.GetDestinationRef() == nil {
		return nil, invalidRequest("destination_ref is required")
	}
	message := store.Message{
		Sender: sender,
		DestinationRef: model.DestinationRef{
			Namespace: request.GetDestinationRef().GetNamespace(), Name: request.GetDestinationRef().GetName(),
			UID: request.GetDestinationRef().GetUid(),
		},
		SessionID: request.GetSessionId(), IdempotencyKey: request.GetIdempotencyKey(), Body: request.GetBody(),
		FromSession: request.FromSession,
	}
	if err := message.Validate(s.messageBodyLimit); err != nil {
		return nil, err
	}
	if !s.policy.Health.Available() {
		return nil, errSendPolicyUnavailable
	}
	view, err := s.sandbox(ctx, message.DestinationRef)
	if err != nil {
		return nil, err
	}
	if !s.policy.PermitsSend(policyapi.ServiceAccountRef(sender), view.Resource) {
		return nil, errSendDenied
	}
	sandbox := view.Identity
	message.DestinationRef.UID = sandbox.UID
	// A Sandbox runs as the ServiceAccount of its own name.
	message.Recipient = model.ServiceAccount{Namespace: sandbox.Namespace, Name: sandbox.Name}
	sent, err := s.store.Send(ctx, message)
	if errors.Is(err, store.ErrNoInbox) {
		if err := s.confirmSession(ctx, sandbox, message.SessionID); err != nil {
			return nil, err
		}
		if err := s.store.OpenInbox(ctx, message.Recipient, message.DestinationRef, message.SessionID); err != nil {
			return nil, err
		}
		sent, err = s.store.Send(ctx, message)
	}
	if err != nil {
		return nil, err
	}
	return &notificationspb.SendMessageResponse{Message: sentMessageView(sent)}, nil
}

// confirmSession checks that the History Service recorded the Session as written by the Sandbox, so that a sender
// cannot spend the recipient's inbox quota on Session IDs it made up. History records the Sandbox by name, so a
// Session of a deleted Sandbox of the same name also passes.
func (s *Notifications) confirmSession(ctx context.Context, sandbox sandboxreader.Identity, sessionID string) error {
	summary, err := s.history.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID})
	switch status.Code(err) {
	case codes.OK:
	// A Session ID History cannot parse is one it never recorded.
	case codes.NotFound, codes.InvalidArgument:
		return errSessionUnrecorded
	default:
		return unavailable("history", err)
	}
	if recorded := summary.GetSandbox(); recorded.GetNamespace() != sandbox.Namespace || recorded.GetName() != sandbox.Name {
		return errSessionUnrecorded
	}
	return nil
}

func (s *Notifications) GetMessage(ctx context.Context, request *notificationspb.GetMessageRequest) (*notificationspb.GetMessageResponse, error) {
	return serve(ctx, s, request, s.getMessage)
}

func (s *Notifications) getMessage(ctx context.Context, sender model.ServiceAccount, request *notificationspb.GetMessageRequest) (*notificationspb.GetMessageResponse, error) {
	id, err := parseID("message_id", request.GetMessageId())
	if err != nil {
		return nil, err
	}
	sent, err := s.store.SentMessage(ctx, sender, id)
	if err != nil {
		return nil, err
	}
	return &notificationspb.GetMessageResponse{Message: sentMessageView(sent)}, nil
}

func sentMessageView(sent store.SentMessage) *notificationspb.Message {
	return &notificationspb.Message{
		Id: sent.ID.String(), DestinationRef: destinationRefView(sent.DestinationRef), SessionId: sent.SessionID,
		IdempotencyKey: sent.IdempotencyKey, AcceptedAt: timestamppb.New(sent.AcceptedAt), Covered: sent.Covered,
		Acknowledged: sent.Acknowledged, InboxRetired: sent.InboxRetired,
	}
}
