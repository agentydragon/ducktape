package api

import (
	"context"
	"errors"
	"fmt"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/sessionidentity"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/types"
)

func (s *Notifications) SendMessage(ctx context.Context, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
	return serve(ctx, s, request, s.sendMessage)
}

// sendMessage resolves the Session's live placement before checking the sender's grant against its exact Sandbox.
// Revoking a grant later never withdraws an accepted message.
func (s *Notifications) sendMessage(ctx context.Context, caller actor, request *notificationspb.SendMessageRequest) (*notificationspb.SendMessageResponse, error) {
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
	// The sender's grant is looked up by the bindings naming it: its ServiceAccount, or its exact Identity
	// incarnation. An Identity sender is named to recipients by the Sandbox it sends from.
	var sender store.Sender
	var grantee reader.Subject
	switch caller := caller.(type) {
	case accountActor:
		sender = store.AccountSender{Account: caller.account, UID: caller.uid}
		grantee = reader.ServiceAccount(policyapi.ServiceAccountRef(caller.account))
	case identityActor:
		sender = store.IdentitySender{Identity: caller.identity, Sandbox: model.ServiceAccount{Namespace: caller.sandbox.Namespace, Name: caller.sandbox.Name}}
		grantee = reader.Identity{Name: caller.identity.Name, UID: types.UID(caller.identity.UID)}
	default:
		panic(fmt.Sprintf("unknown actor %T", caller))
	}
	message := store.Message{
		Sender: sender,
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
	// A Session of the sender's own Identity takes its messages without a grant: the recipient could
	// learn the same itself. Any other recipient needs a `session_inbox_access` rule bound to the sender.
	// TODO(SESSION_IDENTITY_AUTHZ): target that grant at the recipient Session's Identity, not the
	// Sandbox hosting it (agentplane/plans/task_dag.md).
	if !sameIdentity(caller, session.GetSession(), message.SessionID, message.DestinationRef) {
		if !s.policy.Health.Available() {
			return nil, errSendPolicyUnavailable
		}
		if !s.policy.PermitsSend(grantee, view.Resource) {
			return nil, errSendDenied
		}
	}
	sandbox := view.Identity
	message.DestinationRef.UID = sandbox.Uid
	// The recipient is the exact Identity the Session authority recorded, which the destination Sandbox must name.
	if identity, ok := sessionidentity.IdentityRun(session.GetSession(), message.SessionID, message.DestinationRef); ok {
		message.Recipient = store.Owner{Identity: identity, SandboxUID: sandbox.Uid}
	} else {
		return nil, store.ErrNotFound
	}
	runsAs, err := s.sandboxRunsAs(ctx, view, message.Recipient)
	if err != nil {
		return nil, err
	}
	if !runsAs {
		return nil, errSandboxMissing
	}
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

// sameIdentity reports whether caller acts as the Identity incarnation the Session authority recorded the
// recipient Session as executing as, by name and UID: an Identity recreated under the name is another
// sender, as its credentials are another incarnation's.
func sameIdentity(caller actor, session *dispatcherpb.Session, sessionID string, ref model.DestinationRef) bool {
	sender, ok := caller.(identityActor)
	if !ok {
		return false
	}
	recorded, ok := sessionidentity.Identity(session, sessionID, ref)
	return ok && recorded == sender.identity
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

func (s *Notifications) getMessage(ctx context.Context, caller actor, request *notificationspb.GetMessageRequest) (*notificationspb.GetMessageResponse, error) {
	id, err := parseID("message_id", request.GetMessageId())
	if err != nil {
		return nil, err
	}
	var sender store.Sender
	switch caller := caller.(type) {
	case accountActor:
		sender = store.AccountSender{Account: caller.account, UID: caller.uid}
	case identityActor:
		sender = store.IdentitySender{Identity: caller.identity, Sandbox: model.ServiceAccount{Namespace: caller.sandbox.Namespace, Name: caller.sandbox.Name}}
	default:
		panic(fmt.Sprintf("unknown actor %T", caller))
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
