// Package api serves the Notification Service's gRPC API.
//
// Methods admits every RPC's caller before it runs, by a bearer reviewed on that call
// (agentplane/docs/grpc_services.md). A workload acts for its own ServiceAccount and only on what that account owns.
package api

import (
	"context"
	"time"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notificationspb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/postgres/wakeups"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

// Runner lists the Sessions a Sandbox's runner holds.
type Runner interface {
	ListSessions(ctx context.Context, sandbox *sandboxreader.Identity) ([]string, error)
}

// Actions reads the next page of an Action request's events after a sequence, if the account submitted the
// request. A subscription is refused when it cannot, so a caller cannot follow another account's requests.
type Actions interface {
	Events(ctx context.Context, owner model.ServiceAccount, requestID uuid.UUID, afterSequence int64, ownerUID string) ([]storagecodec.ActionEvent, error)
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
	Authorize(ctx context.Context, source model.GitHubSource) (store.GitHubAccessKey, error)
}

// Options are what the servicers read and whom they admit.
type Options struct {
	Store *store.Store
	// Wakeups must listen to store.InboxChannel, which the Session watches follow.
	Wakeups *wakeups.Hub
	// Namespace is the one namespace whose Sandboxes destinations may name, and the one the operator reads.
	Namespace string
	Sandboxes worker.Sandboxes
	Runner    Runner
	Actions   Actions
	// GitHub is nil while the provider is off: a GitHub source is then refused.
	GitHub GitHub
	// SendPolicy is the grants a message is checked against.
	SendPolicy *inboxaccess.Policy
	// History confirms a Session before a message opens an inbox for it.
	History  SessionHistory
	Sessions Sessions
	// MessageBodyLimit is the most UTF-8 bytes a message body may hold, at most store.MessageBodyCeiling.
	MessageBodyLimit int
	// OperatorReader is the name, in Namespace, of the ServiceAccount that can read Session diagnostics for the namespace.
	OperatorReader string
	// Debounce is what the worker waits before it announces entries; the operator's reads report it.
	Debounce store.Debounce
	// RequestTimeout bounds a call's backend work and each write to a watch stream.
	RequestTimeout time.Duration
}

// Methods admits authenticated workloads using the source configured on this listener; individual RPCs
// enforce resource ownership. Passing the listener's source keeps TLS SVID and TokenReview methods on the same
// method policy without coupling either resolver to the servicers' options.
func Methods(options Options, principalResolver auth.PrincipalResolver) auth.Methods {
	workloads := auth.AnyAccount(principalResolver)
	return auth.Methods{
		notificationspb.Notifications_CreateSubscription_FullMethodName: workloads,
		notificationspb.Notifications_ListSubscriptions_FullMethodName:  workloads,
		notificationspb.Notifications_GetSubscription_FullMethodName:    workloads,
		notificationspb.Notifications_RenewSubscription_FullMethodName:  workloads,
		notificationspb.Notifications_CancelSubscription_FullMethodName: workloads,
		notificationspb.Notifications_ListInboxes_FullMethodName:        workloads,
		notificationspb.Notifications_GetInbox_FullMethodName:           workloads,
		notificationspb.Notifications_WatchInbox_FullMethodName:         workloads,
		notificationspb.Notifications_WatchInboxEntries_FullMethodName:  workloads,
		notificationspb.Notifications_WatchSubscriptions_FullMethodName: workloads,
		notificationspb.Notifications_ReadInbox_FullMethodName:          workloads,
		notificationspb.Notifications_AcknowledgeInbox_FullMethodName:   workloads,
		notificationspb.Notifications_RetireInbox_FullMethodName:        workloads,
		notificationspb.Notifications_SendMessage_FullMethodName:        workloads,
		notificationspb.Notifications_GetMessage_FullMethodName:         workloads,
	}
}
