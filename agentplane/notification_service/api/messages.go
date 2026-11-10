package api

import (
	"context"
	"errors"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func (s *Notifications) SendMessage(ctx context.Context, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	return serve(ctx, s, request, s.sendMessage)
}

// sendMessage resolves the Session's live placement before checking the sender's grant against its exact Sandbox.
// Revoking a grant later never withdraws an accepted message.
func (s *Notifications) sendMessage(ctx context.Context, sender model.ServiceAccount, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	// Native gRPC retains unknown fields. Reject the retired Sandbox address so an old client's
	// explicit name or UID fence cannot be silently discarded by the new Session-only request.
	for unknown := request.ProtoReflect().GetUnknown(); len(unknown) > 0; {
		field, _, size := protowire.ConsumeField(unknown)
		if size < 0 {
			return nil, invalidRequest("invalid message request")
		}
		if field == 1 {
			return nil, invalidRequest("destination_ref is no longer accepted; address the Session by ID")
		}
		unknown = unknown[size:]
	}
	if err := model.CheckText("session_id", request.GetSessionId(), 1, 200); err != nil {
		return nil, err
	}
	if !s.policy.Health.Available() {
		return nil, errSendPolicyUnavailable
	}
	session, err := s.sessions.GetSession(ctx, &dispatcherpb.GetSessionRequest{SessionId: request.GetSessionId()})
	if status.Code(err) == codes.NotFound {
		return nil, errNoSession
	}
	if err != nil {
		return nil, unavailable("Session", err)
	}
	target := sessionidentity.Target(session.GetSession(), request.GetSessionId())
	if target == nil {
		return nil, unavailable("Session", errors.New("incomplete Session placement"))
	}
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, admissionRefused{err}
	}
	message := store.Message{
		SenderUID: principal.ServiceAccountUID,
		Sender:    sender,
		DestinationRef: model.DestinationRef{
			Namespace: s.namespace, Name: target.GetName(), UID: target.GetUid(),
		},
		SessionID: request.GetSessionId(), IdempotencyKey: request.GetIdempotencyKey(), Body: request.GetBody(),
		FromSession: request.FromSession,
	}
	if err := message.Validate(s.messageBodyLimit); err != nil {
		return nil, err
	}
	view, err := s.sandbox(ctx, message.DestinationRef)
	if err != nil {
		return nil, err
	}
	if !s.policy.PermitsSend(policyapi.ServiceAccountRef(sender), view.Resource) {
		return nil, errSendDenied
	}
	sandbox := view.Identity
	message.DestinationRef.UID = sandbox.Uid
	// A Sandbox runs as the ServiceAccount of its own name.
	message.Recipient = model.ServiceAccount{Namespace: sandbox.Namespace, Name: sandbox.Name}
	message.RecipientUID, err = sessionidentity.AccountUID(session.GetSession(), message.SessionID, message.DestinationRef, message.Recipient)
	if err != nil {
		return nil, err
	}
	if err := s.store.EstablishInboxOwner(ctx, message.Recipient, message.DestinationRef, message.SessionID, message.RecipientUID); err != nil {
		return nil, err
	}
	sent, err := s.store.Send(ctx, message)
	if errors.Is(err, store.ErrNoInbox) {
		if err := s.confirmSession(ctx, sandbox, message.SessionID); err != nil {
			return nil, err
		}
		if err := s.store.OpenInbox(ctx, message.Recipient, message.DestinationRef, message.SessionID, message.RecipientUID); err != nil {
			return nil, err
		}
		sent, err = s.store.Send(ctx, message)
	}
	if err != nil {
		return nil, err
	}
	if err := s.establishIdentity(ctx, session.GetSession(), message.DestinationRef, message.SessionID); err != nil {
		return nil, err
	}
	return &notificationspb.SendMessageResponse{Message: sentMessageView(sent)}, nil
}

// confirmSession checks that History recorded the Session against this exact Sandbox incarnation, so a sender
// cannot spend the recipient's inbox quota on a Session ID it made up or one written by a deleted Sandbox with
// the same name. Both sides need a UID: History records without one cannot establish this association.
func (s *Notifications) confirmSession(ctx context.Context, sandbox *sandboxreader.Identity, sessionID string) error {
	summary, err := s.history.GetSessionSummary(ctx, &historypb.GetSessionSummaryRequest{SessionId: sessionID})
	switch status.Code(err) {
	case codes.OK:
	// A Session ID History cannot parse is one it never recorded.
	case codes.NotFound, codes.InvalidArgument:
		return errSessionUnrecorded
	default:
		return unavailable("history", err)
	}
	recorded := summary.GetSandbox()
	if sandbox.GetUid() == "" || recorded.GetUid() == "" ||
		recorded.GetNamespace() != sandbox.GetNamespace() || recorded.GetName() != sandbox.GetName() ||
		recorded.GetUid() != sandbox.GetUid() {
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
	principal, err := auth.Caller(ctx)
	if err != nil {
		return nil, admissionRefused{err}
	}
	sent, err := s.store.SentMessage(ctx, sender, id, principal.ServiceAccountUID)
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
