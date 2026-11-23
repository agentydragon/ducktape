package rpc

import (
	"context"
	"errors"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	inboxstore "github.com/agentydragon/ducktape/agentplane/notification_service/internal/inbox"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/sessionidentity"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/types"
)

func (s *Notifications) SendMessage(ctx context.Context, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	return serve(ctx, s, request, s.sendMessage)
}

// sendMessage resolves the Session's live placement before checking the sender's grant against its exact Sandbox.
// Revoking a grant later never withdraws an accepted message.
func (s *Notifications) sendMessage(ctx context.Context, caller identityActor, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	if caller.sandbox.Namespace == "" || caller.sandbox.Name == "" || caller.sandbox.UID == "" {
		return nil, errSandboxMissing
	}
	if err := rejectRetiredAddress(request, "message", "destination_ref"); err != nil {
		return nil, err
	}
	if err := model.CheckText("session_id", request.GetSessionId(), 1, 200); err != nil {
		return nil, err
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
	// Grants and attribution use the authenticated Identity incarnation.
	sender := inboxstore.IdentitySender{Identity: caller.identity, Sandbox: model.ServiceAccount{Namespace: caller.sandbox.Namespace, Name: caller.sandbox.Name}}
	grantee := reader.Identity{Name: caller.identity.Name, UID: types.UID(caller.identity.UID)}
	// destination authorizes the send and pins the Sandbox incarnation; the inbox itself is keyed by
	// Session ID, so nothing stores it.
	destination := model.DestinationRef{Namespace: s.namespace, Name: target.GetName(), UID: target.GetUid()}
	message := inboxstore.Message{
		Sender: sender, SessionID: request.GetSessionId(), IdempotencyKey: request.GetIdempotencyKey(),
		Body: request.GetBody(), FromSession: request.FromSession,
	}
	if err := message.Validate(s.messageBodyLimit); err != nil {
		return nil, err
	}
	view, err := s.sandbox(ctx, destination)
	if err != nil {
		return nil, err
	}
	// A Session of the sender's own Identity takes its messages without a grant: the recipient could
	// learn the same itself. Any other recipient needs a `session_inbox_access` rule bound to the sender.
	// TODO(SESSION_IDENTITY_AUTHZ): target that grant at the recipient Session's Identity, not the
	// Sandbox hosting it (agentplane/plans/task_dag.md).
	if !sameIdentity(caller, session.GetSession(), message.SessionID, destination) {
		if !s.policy.Health.Available() {
			return nil, errSendPolicyUnavailable
		}
		if !s.policy.PermitsSend(grantee, view.Resource) {
			return nil, errSendDenied
		}
	}
	sandbox := view.Identity
	destination.UID = sandbox.Uid
	// The recipient is the exact Identity the Session authority recorded, which the destination Sandbox must name.
	if identity, ok := sessionidentity.Identity(session.GetSession(), message.SessionID, destination); ok {
		message.Recipient = inboxstore.Owner{Identity: identity}
	} else {
		return nil, inboxstore.ErrNotFound
	}
	runsAs, err := s.sandboxRunsAs(ctx, view, message.Recipient)
	if err != nil {
		return nil, err
	}
	if !runsAs {
		return nil, errSandboxMissing
	}
	sent, err := s.store.Send(ctx, message)
	if errors.Is(err, inboxstore.ErrNoInbox) {
		if err := s.confirmSession(ctx, sandbox, message.SessionID); err != nil {
			return nil, err
		}
		if err := s.store.OpenInbox(ctx, message.Recipient, message.SessionID); err != nil {
			return nil, err
		}
		sent, err = s.store.Send(ctx, message)
	}
	if err != nil {
		return nil, err
	}
	return &notificationspb.SendMessageResponse{Message: sentMessageView(sent)}, nil
}

// sameIdentity reports whether caller acts as the Identity incarnation the Session authority recorded the
// recipient Session as executing as, by name and UID: an Identity recreated under the name is another
// sender, as its credentials are another incarnation's.
func sameIdentity(caller identityActor, session *dispatcherpb.Session, sessionID string, ref model.DestinationRef) bool {
	recorded, ok := sessionidentity.Identity(session, sessionID, ref)
	return ok && recorded == caller.identity
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

func (s *Notifications) getMessage(ctx context.Context, caller identityActor, request *notificationspb.GetMessageRequest) (*notificationspb.GetMessageResponse, error) {
	if caller.sandbox.Namespace == "" || caller.sandbox.Name == "" {
		return nil, inboxstore.ErrNotFound
	}
	id, err := parseID("message_id", request.GetMessageId())
	if err != nil {
		return nil, err
	}
	sender := inboxstore.IdentitySender{Identity: caller.identity, Sandbox: model.ServiceAccount{Namespace: caller.sandbox.Namespace, Name: caller.sandbox.Name}}
	sent, err := s.store.SentMessage(ctx, sender, id)
	if err != nil {
		return nil, err
	}
	return &notificationspb.GetMessageResponse{Message: sentMessageView(sent)}, nil
}

func sentMessageView(sent inboxstore.SentMessage) *notificationspb.Message {
	return &notificationspb.Message{
		Id: sent.ID.String(), SessionId: sent.SessionID,
		IdempotencyKey: sent.IdempotencyKey, AcceptedAt: timestamppb.New(sent.AcceptedAt), Covered: sent.Covered,
		Acknowledged: sent.Acknowledged, InboxRetired: sent.InboxRetired,
	}
}
