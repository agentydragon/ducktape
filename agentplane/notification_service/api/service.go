// Package api serves the Notification Service's gRPC APIs: `Notifications`, which workloads call for their own
// subscriptions and inboxes, and `NotificationsOperator`, which the integration app calls to show an operator
// what a Sandbox's notifications are doing.
//
// Every RPC admits its caller first, by a bearer reviewed on that call (agentplane/docs/grpc_services.md). A
// workload acts for its own ServiceAccount and only on what that account owns.
package api

import (
	"context"
	"time"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/inboxaccess"
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/agentydragon/ducktape/agentplane/notification_service/worker"
	"github.com/agentydragon/ducktape/agentplane/postgres/go/wakeups"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

// Runner lists the Sessions a Sandbox's runner holds.
type Runner interface {
	ListSessions(ctx context.Context, sandbox sandboxreader.Identity) ([]string, error)
}

// Actions reads the next page of an Action request's events after a sequence, if the account submitted the
// request. A subscription is refused when it cannot, so a caller cannot follow another account's requests.
type Actions interface {
	Events(ctx context.Context, owner model.ServiceAccount, requestID uuid.UUID, afterSequence int64) ([]store.ActionEvent, error)
}

// SessionHistory reads the Sandbox the History Service recorded as writing a Session.
type SessionHistory interface {
	GetSessionSummary(ctx context.Context, in *historypb.GetSessionSummaryRequest, opts ...grpc.CallOption) (*historypb.SessionSummary, error)
}

// GitHub authorizes a GitHub source: it returns the access the service's GitHub App reads the source under, or
// the client's typed errors (package github/client) when it cannot.
type GitHub interface {
	Authorize(ctx context.Context, source model.GitHubSource) (store.GitHubAccessKey, error)
}

// Options are what the servicers read and whom they admit.
type Options struct {
	Store *store.Store
	// Wakeups must listen to store.WorkChannel, which the operator's watches follow.
	Wakeups *wakeups.Hub
	// Resolver reviews a caller's bearer; it admits only the namespaces the Sandboxes run in.
	Resolver *auth.Resolver
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
	History SessionHistory
	// MessageBodyLimit is the most UTF-8 bytes a message body may hold, at most store.MessageBodyCeiling.
	MessageBodyLimit int
	// OperatorReader is the name, in Namespace, of the ServiceAccount that may call NotificationsOperator. Empty
	// refuses every caller.
	OperatorReader string
	// Debounce is what the worker waits before it announces entries; the operator's reads report it.
	Debounce store.Debounce
	// RequestTimeout bounds a call's backend work and each write to a watch stream.
	RequestTimeout time.Duration
	// WatchLease ends each watch after this long: a stream is admitted once, so the lease bounds how long a
	// revoked token keeps reading.
	WatchLease time.Duration
}
