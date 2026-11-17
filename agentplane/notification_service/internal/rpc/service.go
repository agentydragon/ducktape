// Package rpc serves the Notification Service's gRPC API.
//
// Methods admits workload SVIDs and exact service SVID diagnostic grants before each RPC.
// Workloads act for the live Identity their credential names.
package rpc

import (
	"context"
	"time"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/notification_service/api/notifications/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/inboxaccess"
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/worker"

	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

// Runner lists the Sessions a Sandbox's runner holds.
type Runner interface {
	ListSessions(ctx context.Context, sandbox *sandboxreader.Identity) ([]string, error)
}

// Actions reads the next page of an Action request's events after a sequence, if the owner's
// Identity incarnation submitted the request. A subscription is refused when it cannot, so a caller cannot follow
// another's requests.
type Actions interface {
	Events(ctx context.Context, owner store.Owner, requestID uuid.UUID, afterSequence int64) ([]storagecodec.ActionEvent, error)
}

// SessionHistory reads the Sandbox the History Service recorded as writing a Session.
type SessionHistory interface {
	GetSessionSummary(ctx context.Context, in *historypb.GetSessionSummaryRequest, opts ...grpc.CallOption) (*historypb.SessionSummary, error)
}

type Sessions interface {
	GetSession(context.Context, *dispatcherpb.GetSessionRequest, ...grpc.CallOption) (*dispatcherpb.GetSessionResponse, error)
}

// GitHub authorizes a GitHub source: it returns the access the service's GitHub App reads the source under, or
// the client's typed errors (package github/client) when it cannot.
type GitHub interface {
	Authorize(ctx context.Context, source githubmodel.Source) (githubstate.AccessKey, error)
}

// Options are what the servicers read and whom they admit.
type Options struct {
	Store *store.Store
	// Wakeups must listen to store.InboxChannel, which the Session watches follow.
	Wakeups *wakeups.Hub
	// Namespace is the one namespace whose Sandboxes destinations may name, and the one the operator reads.
	Namespace       string
	Sandboxes       worker.Sandboxes
	SandboxSessions Runner
	Actions         Actions
	// GitHub is nil while the provider is off: a GitHub source is then refused.
	GitHub GitHub
	// SendPolicy is the grants a message is checked against.
	SendPolicy *inboxaccess.Policy
	// History confirms a Session before a message opens an inbox for it.
	History  SessionHistory
	Sessions Sessions
	// MessageBodyLimit is the most UTF-8 bytes a message body may hold, at most store.MessageBodyCeiling.
	MessageBodyLimit int
	// OperatorReaderServices are exact component SVIDs allowed to read Session diagnostics, independently of agent Identity admission.
	OperatorReaderServices []string
	// Debounce is what the worker waits before it announces entries; the operator's reads report it.
	Debounce store.Debounce
	// RequestTimeout bounds a call's backend work and each write to a watch stream.
	RequestTimeout time.Duration
}

// Methods admits live workload Identities; handlers enforce resource ownership.
// Only the named diagnostic methods additionally admit exact configured service SVIDs.
func Methods(options Options, resolver *auth.SVIDResolver) (auth.Methods, error) {
	owners := auth.AnySubject(resolver)
	methods := auth.Methods{
		notificationspb.Notifications_CreateSubscription_FullMethodName: owners,
		notificationspb.Notifications_ListSubscriptions_FullMethodName:  owners,
		notificationspb.Notifications_GetSubscription_FullMethodName:    owners,
		notificationspb.Notifications_RenewSubscription_FullMethodName:  owners,
		notificationspb.Notifications_CancelSubscription_FullMethodName: owners,
		notificationspb.Notifications_ListInboxes_FullMethodName:        owners,
		notificationspb.Notifications_GetInbox_FullMethodName:           owners,
		notificationspb.Notifications_WatchInbox_FullMethodName:         owners,
		notificationspb.Notifications_WatchInboxEntries_FullMethodName:  owners,
		notificationspb.Notifications_WatchSubscriptions_FullMethodName: owners,
		notificationspb.Notifications_ReadInbox_FullMethodName:          owners,
		notificationspb.Notifications_AcknowledgeInbox_FullMethodName:   owners,
		notificationspb.Notifications_RetireInbox_FullMethodName:        owners,
		notificationspb.Notifications_SendMessage_FullMethodName:        owners,
		notificationspb.Notifications_GetMessage_FullMethodName:         owners,
	}
	return methods.WithServiceCallers(operatorReaderMethods(), options.OperatorReaderServices)
}

func operatorReaderMethods() []string {
	return []string{
		notificationspb.Notifications_GetInbox_FullMethodName,
		notificationspb.Notifications_WatchInbox_FullMethodName,
		notificationspb.Notifications_WatchInboxEntries_FullMethodName,
		notificationspb.Notifications_WatchSubscriptions_FullMethodName,
		notificationspb.Notifications_ReadInbox_FullMethodName,
	}
}
