package main

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	"github.com/agentydragon/ducktape/agentplane/app/go/liveinventory"
	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	"github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/notification_service/notifications_operatorpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	corev1 "k8s.io/api/core/v1"
)

const (
	liveHealthInterval = 15 * time.Second
	maxNoticeThreads   = 200
)

type streamingUpstream interface {
	ForwardHTTP(http.ResponseWriter, *http.Request, context.Context, string, string) error
}

type sandboxNotificationWatcher interface {
	WatchSandboxNotifications(context.Context, *notifications_operatorpb.WatchSandboxNotificationsRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[notifications_operatorpb.WatchSandboxNotificationsResponse], error)
	WatchSessionNotices(context.Context, *notifications_operatorpb.WatchSessionNoticesRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[notifications_operatorpb.WatchSessionNoticesResponse], error)
}

type egressDecisionWatcher interface {
	WatchDecisions(context.Context, *egress_adminpb.WatchDecisionsRequest, ...grpc.CallOption) (grpc.ServerStreamingClient[egress_adminpb.WatchDecisionsResponse], error)
}

type streamLiveInventory interface {
	Snapshot(context.Context) (liveinventory.Snapshot, error)
	Subscribe() (<-chan struct{}, func())
}

type streamSandboxInventory interface {
	Get(context.Context, string) (*sandboxespb.Sandbox, error)
}

type streamThreadStore interface {
	threadReadStore
	ArchivedCount(context.Context) (int, error)
}

type streamHistoryStore interface {
	threadReadHistory
	ReadWatermark(context.Context, uuid.UUID) (uint64, error)
}

// StreamingDependencies are the narrow runtime interfaces used by SSE and raw upstream routes.
// All route authorization still goes through HTTPServer.RegisterRawRoute.
type StreamingDependencies struct {
	Auth             *auth.Auth
	Updates          auth.ChangeSource
	UpdatesConnected func() bool
	Live             streamLiveInventory
	Sandboxes        streamSandboxInventory
	Threads          streamThreadStore
	History          streamHistoryStore
	HistoryFollower  interface {
		FollowSession(context.Context, *historypb.FollowSessionRequest) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error)
	}
	ActionPolicy     policyaccess.ActionPolicyRPC
	Federator        policyaccess.ActionFederator
	ActionServiceURL string
	DispatcherHTTP   streamingUpstream
	ActionHTTP       streamingUpstream
	ConnectionHTTP   streamingUpstream
	Notifications    sandboxNotificationWatcher
	NotificationWait time.Duration
	Egress           egressDecisionWatcher
	EgressWait       time.Duration
	ThreadHeartbeat  time.Duration
	Console          sandboxes.ConsoleSource
	Logger           *slog.Logger
}

// RegisterStreamingRoutes installs the live snapshots, event/notice/console streams, and
// operator-facing HTTP facades. The server owns the shared auth/session middleware and drain gate.
func RegisterStreamingRoutes(server *HTTPServer, dependencies StreamingDependencies) {
	routes := &streamingRoutes{server: server, deps: dependencies}
	server.RegisterRawRoute("GET /live/sandboxes", http.HandlerFunc(routes.liveSandboxes), false)
	server.RegisterRawRoute("GET /live/threads", http.HandlerFunc(routes.liveThreads), false)
	server.RegisterRawRoute("GET /live/sandboxes/{name}", http.HandlerFunc(routes.liveSandbox), false)
	server.RegisterRawRoute("GET /live/sandboxes/{name}/egress/decisions", http.HandlerFunc(routes.liveDecisions), false)
	server.RegisterRawRoute("GET /threads/{thread_id}/events/stream", http.HandlerFunc(routes.threadEvents), false)
	server.RegisterRawRoute("GET /threads/notices/stream", http.HandlerFunc(routes.threadNotices), true)
	server.RegisterRawRoute("GET /sandboxes/{name}/notifications/stream", http.HandlerFunc(routes.sandboxNotifications), true)
	server.RegisterRawRoute("GET /sandboxes/{name}/console/stream", http.HandlerFunc(routes.console), true)
	server.RegisterRawRoute("GET /upstream/dispatcher/{path...}", http.HandlerFunc(routes.dispatcher), true)
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodPatch, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/actions/{path...}", http.HandlerFunc(routes.actionFacade), true)
		server.RegisterRawRoute(method+" /upstream/connections/{path...}", http.HandlerFunc(routes.connectionFacade), true)
	}
}

type streamingRoutes struct {
	server *HTTPServer
	deps   StreamingDependencies
}

// watchHealthView is the live inventory's health: healthy once its informers have synced and while no watched kind
// fails to list or watch, and each failing kind with its error and since when.
type watchHealthView struct {
	Healthy bool                       `json:"healthy"`
	Failing map[string]kindFailureView `json:"failing"`
}

type kindFailureView struct {
	Error string    `json:"error"`
	Since time.Time `json:"since"`
}

type sandboxListFrame struct {
	Sandboxes []SandboxView   `json:"sandboxes"`
	Watch     watchHealthView `json:"watch"`
}

type liveThreadsFrame struct {
	Sandboxes        []SandboxView   `json:"sandboxes"`
	Threads          []threadView    `json:"threads"`
	More             bool            `json:"more"`
	ArchivedCount    int             `json:"archived_count"`
	UpdatesConnected bool            `json:"updates_connected"`
	Watch            watchHealthView `json:"watch"`
}

type liveSandboxFrame struct {
	Sandbox          *SandboxView               `json:"sandbox"`
	Bindings         []policyaccess.BindingView `json:"bindings"`
	ActionPolicy     any                        `json:"action_policy"`
	Threads          any                        `json:"threads"`
	UpdatesConnected bool                       `json:"updates_connected"`
	Watch            watchHealthView            `json:"watch"`
}

type liveDecisionsFrame struct {
	Watching  bool `json:"watching"`
	Decisions any  `json:"decisions"`
}

type decisionsUnavailable struct {
	Detail string `json:"detail"`
}

type decisionView struct {
	At          time.Time `json:"at"`
	Method      string    `json:"method"`
	Host        string    `json:"host"`
	Port        int32     `json:"port"`
	Outcome     string    `json:"outcome"`
	Reason      *string   `json:"reason"`
	Binding     *string   `json:"binding"`
	Policy      *string   `json:"policy"`
	Rule        *int32    `json:"rule"`
	Substituted bool      `json:"substituted"`
	Address     *string   `json:"address"`
}

func (r *streamingRoutes) logger() *slog.Logger {
	if r.deps.Logger != nil {
		return r.deps.Logger
	}
	return slog.Default()
}

func (r *streamingRoutes) liveSandboxes(w http.ResponseWriter, request *http.Request) {
	if r.deps.Live == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "live inventory is unavailable")
		return
	}
	ctx, cancel := r.requestLifetime(request)
	defer cancel()
	changes, unsubscribe := r.deps.Live.Subscribe()
	defer unsubscribe()
	r.streamSnapshots(w, request, ctx, changes, func(ctx context.Context) (any, error) {
		views, health, err := r.sandboxViews(ctx)
		if err != nil {
			return nil, err
		}
		return sandboxListFrame{Sandboxes: views, Watch: health}, nil
	}, func() any {
		_, health, err := r.sandboxViews(request.Context())
		if err != nil {
			return nil
		}
		return health
	})
}

func (r *streamingRoutes) liveThreads(w http.ResponseWriter, request *http.Request) {
	if r.deps.Live == nil || r.deps.Threads == nil || r.deps.History == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "live thread inventory is unavailable")
		return
	}
	limit := 50
	if text := request.URL.Query().Get("limit"); text != "" {
		value, err := strconv.Atoi(text)
		if err != nil || value < 1 || value > threadMaxPageSize {
			writeDetailError(w, http.StatusUnprocessableEntity, "limit must be between 1 and 200")
			return
		}
		limit = value
	}
	search := request.URL.Query().Get("search")
	var searchPtr *string
	if _, present := request.URL.Query()["search"]; present {
		if search == "" {
			writeDetailError(w, http.StatusUnprocessableEntity, "search must contain at least 1 character")
			return
		}
		searchPtr = &search
	}
	includeArchived, err := parseOptionalBool(request.URL.Query(), "include_archived", false)
	if err != nil {
		writeDetailError(w, http.StatusUnprocessableEntity, "include_archived must be a boolean")
		return
	}
	var openThread *uuid.UUID
	if value := request.URL.Query().Get("open_thread"); value != "" {
		id, parseErr := uuid.Parse(value)
		if parseErr != nil {
			writeDetailError(w, http.StatusUnprocessableEntity, "open_thread must be a UUID")
			return
		}
		openThread = &id
	}
	ctx, cancel := r.requestLifetime(request)
	defer cancel()
	liveChanges, stopLive := r.deps.Live.Subscribe()
	defer stopLive()
	var updateChanges <-chan struct{}
	var stopUpdates func()
	if r.deps.Updates != nil {
		updateChanges, stopUpdates, err = r.deps.Updates.Subscribe(threads.UpdatesChannel)
		if err != nil {
			writeDetailError(w, http.StatusServiceUnavailable, "Thread updates are unavailable")
			return
		}
		defer stopUpdates()
	}
	r.streamSnapshots(w, request, ctx, liveChanges, func(ctx context.Context) (any, error) {
		views, health, err := r.sandboxViews(ctx)
		if err != nil {
			return nil, err
		}
		page, err := listThreadViews(ctx, r.deps.Threads, r.deps.History, threadListing{Search: searchPtr, IncludeArchived: includeArchived}, limit, nil)
		if err != nil {
			return nil, err
		}
		if openThread != nil {
			found := false
			for _, thread := range page.Threads {
				if thread.ID == *openThread {
					found = true
					break
				}
			}
			if !found {
				if opened, getErr := getThreadView(ctx, r.deps.Threads, r.deps.History, *openThread); getErr == nil {
					page.Threads = append(page.Threads, opened)
				}
			}
		}
		archived, err := r.deps.Threads.ArchivedCount(ctx)
		if err != nil {
			return nil, err
		}
		connected := r.deps.UpdatesConnected != nil && r.deps.UpdatesConnected()
		return liveThreadsFrame{Sandboxes: views, Threads: page.Threads, More: page.NextPageToken != nil, ArchivedCount: archived, UpdatesConnected: connected, Watch: health}, nil
	}, func() any {
		_, health, _ := r.sandboxViews(request.Context())
		return health
	}, updateChanges)
}

func (r *streamingRoutes) liveSandbox(w http.ResponseWriter, request *http.Request) {
	if r.deps.Live == nil || r.deps.Threads == nil || r.deps.History == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "live sandbox view is unavailable")
		return
	}
	name := request.PathValue("name")
	includeArchived, err := parseOptionalBool(request.URL.Query(), "include_archived", false)
	if err != nil {
		writeDetailError(w, http.StatusUnprocessableEntity, "include_archived must be a boolean")
		return
	}
	ctx, cancel := r.requestLifetime(request)
	defer cancel()
	liveChanges, stopLive := r.deps.Live.Subscribe()
	defer stopLive()
	var updateChanges <-chan struct{}
	var stopUpdates func()
	if r.deps.Updates != nil {
		updateChanges, stopUpdates, err = r.deps.Updates.Subscribe(threads.UpdatesChannel)
		if err != nil {
			writeDetailError(w, http.StatusServiceUnavailable, "Thread updates are unavailable")
			return
		}
		defer stopUpdates()
	}
	var cachedSubject *sandboxespb.ServiceAccount
	var cachedGeneration uint64
	var cachedPolicy any
	hasCachedPolicy := false
	r.streamSnapshots(w, request, ctx, liveChanges, func(ctx context.Context) (any, error) {
		snapshot, err := r.deps.Live.Snapshot(ctx)
		if err != nil {
			return nil, err
		}
		watch := healthView(snapshot.Watch)
		resource, pod := snapshotSandbox(snapshot, name)
		var view *SandboxView
		var bindings []policyaccess.BindingView
		var actionPolicy any
		if resource != nil {
			projected, err := sandboxes.View(resource, pod)
			if err != nil {
				return nil, err
			}
			row := sandboxView(projected)
			view = &row
			bindings, err = policyaccess.BindingsFromSnapshot(snapshot.PolicyBindings, snapshot.PolicySets, snapshot.EgressCredentials, projected.GetServiceAccount())
			if err != nil {
				return nil, err
			}
			subject := projected.GetServiceAccount()
			if hasCachedPolicy && cachedGeneration == snapshot.PolicyGeneration && proto.Equal(cachedSubject, subject) {
				actionPolicy = cachedPolicy
			} else {
				caller, _ := auth.CallerFromContext(request.Context())
				session, _ := auth.SessionFromContext(request.Context())
				api := operatorRoutes{federator: r.deps.Federator, actions: r.deps.ActionPolicy, actionServiceURL: r.deps.ActionServiceURL}
				actionPolicy = api.actionPolicyFrame(ctx, caller, session, subject)
				if _, unavailable := actionPolicy.(*policyaccess.ActionPolicyUnavailable); !unavailable {
					cachedSubject = proto.Clone(subject).(*sandboxespb.ServiceAccount)
					cachedGeneration = snapshot.PolicyGeneration
					cachedPolicy = actionPolicy
					hasCachedPolicy = true
				} else {
					hasCachedPolicy = false
				}
			}
		}
		threadsView, err := r.sandboxThreads(ctx, resource, includeArchived)
		if err != nil {
			return nil, err
		}
		return liveSandboxFrame{
			Sandbox: view, Bindings: nonNilBindingViews(bindings), ActionPolicy: actionPolicy,
			Threads: threadsView, UpdatesConnected: r.deps.UpdatesConnected != nil && r.deps.UpdatesConnected(), Watch: watch,
		}, nil
	}, func() any {
		snapshot, err := r.deps.Live.Snapshot(request.Context())
		if err != nil {
			return nil
		}
		return healthView(snapshot.Watch)
	}, updateChanges)
}

func (r *streamingRoutes) liveDecisions(w http.ResponseWriter, request *http.Request) {
	if r.deps.Live == nil || r.deps.Egress == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "egress decision stream is unavailable")
		return
	}
	ctx, cancel := r.requestLifetime(request)
	defer cancel()
	changes, unsubscribe := r.deps.Live.Subscribe()
	defer unsubscribe()
	name := request.PathValue("name")
	if err := startSSE(w); err != nil {
		return
	}
	for {
		snapshot, err := r.deps.Live.Snapshot(ctx)
		if err != nil {
			return
		}
		resource, pod := snapshotSandbox(snapshot, name)
		if resource == nil {
			if writeSSE(w, "snapshot", liveDecisionsFrame{Watching: false, Decisions: decisionsUnavailable{Detail: "no sandbox " + name}}, nil) != nil {
				return
			}
			select {
			case <-ctx.Done():
				return
			case <-changes:
				continue
			}
		}
		projected, err := sandboxes.View(resource, pod)
		if err != nil {
			return
		}
		watchCtx, stopWatch := context.WithCancel(ctx)
		stream, err := r.deps.Egress.WatchDecisions(watchCtx, decisionsRequest(projected.GetServiceAccount()))
		if err != nil {
			stopWatch()
			if writeSSE(w, "snapshot", liveDecisionsFrame{Watching: false, Decisions: decisionsUnavailable{Detail: grpcFailure(err)}}, nil) != nil {
				return
			}
			if !waitContext(ctx, time.Second) {
				return
			}
			continue
		}
		type decisionResult struct {
			message *egress_adminpb.WatchDecisionsResponse
			err     error
		}
		received := make(chan decisionResult, 1)
		go func() {
			for {
				message, recvErr := stream.Recv()
				select {
				case received <- decisionResult{message: message, err: recvErr}:
				case <-watchCtx.Done():
					return
				}
				if recvErr != nil {
					return
				}
			}
		}()
		first := true
		watching := false
		wait := r.deps.EgressWait
		if wait <= 0 {
			wait = 5 * time.Second
		}
		timer := time.NewTimer(wait)
		streamEnded := false
		for {
			select {
			case <-ctx.Done():
				timer.Stop()
				stopWatch()
				return
			case <-timer.C:
				if first {
					_ = writeSSE(w, "snapshot", liveDecisionsFrame{Watching: false, Decisions: decisionsUnavailable{Detail: fmt.Sprintf("no initial snapshot within %s", compactDuration(wait))}}, nil)
					streamEnded = true
					break
				}
				if writeSSE(w, "health", struct {
					Watching bool `json:"watching"`
				}{watching}, nil) != nil {
					timer.Stop()
					stopWatch()
					return
				}
				timer.Reset(liveHealthInterval)
			case result := <-received:
				if result.err != nil {
					if errors.Is(result.err, io.EOF) {
						streamEnded = true
						break
					}
					if ctx.Err() != nil {
						timer.Stop()
						stopWatch()
						return
					}
					if writeSSE(w, "snapshot", liveDecisionsFrame{Watching: false, Decisions: decisionsUnavailable{Detail: grpcFailure(result.err)}}, nil) != nil {
						timer.Stop()
						stopWatch()
						return
					}
					streamEnded = true
					break
				}
				first = false
				watching = true
				if !timer.Stop() {
					select {
					case <-timer.C:
					default:
					}
				}
				timer.Reset(liveHealthInterval)
				if writeSSE(w, "snapshot", liveDecisionsFrame{Watching: true, Decisions: decisionViews(result.message.GetDecisions())}, nil) != nil {
					timer.Stop()
					stopWatch()
					return
				}
			}
			if streamEnded {
				break
			}
		}
		timer.Stop()
		stopWatch()
		if ctx.Err() != nil {
			return
		}
		_ = watching
		if !waitContext(ctx, time.Second) {
			return
		}
	}
}

func (r *streamingRoutes) sandboxThreads(ctx context.Context, sandbox *sandboxapi.Sandbox, includeArchived bool) (any, error) {
	if sandbox == nil || sandbox.UID == "" {
		return []threadView{}, nil
	}
	sandboxUID := string(sandbox.UID)
	listing := threadListing{SandboxUID: &sandboxUID, IncludeArchived: includeArchived}
	var all []threadView
	var token *string
	for {
		page, err := listThreadViews(ctx, r.deps.Threads, r.deps.History, listing, threadMaxPageSize, token)
		if err != nil {
			code := status.Code(err)
			if code == codes.Unknown {
				code = codes.Unavailable
			}
			return map[string]any{"kind": "unavailable", "code": strings.ToUpper(code.String())}, nil
		}
		all = append(all, page.Threads...)
		if page.NextPageToken == nil {
			if all == nil {
				all = []threadView{}
			}
			return all, nil
		}
		token = page.NextPageToken
	}
}

func (r *streamingRoutes) sandboxViews(ctx context.Context) ([]SandboxView, watchHealthView, error) {
	snapshot, err := r.deps.Live.Snapshot(ctx)
	if err != nil {
		return nil, watchHealthView{}, err
	}
	views := make([]SandboxView, 0, len(snapshot.Sandboxes))
	pods := make(map[string]*corev1.Pod, len(snapshot.Pods))
	for i := range snapshot.Pods {
		pod := snapshot.Pods[i]
		pods[pod.Namespace+"/"+pod.Name] = &pod
	}
	for i := range snapshot.Sandboxes {
		resource := &snapshot.Sandboxes[i]
		var pod *corev1.Pod
		if incarnation := resource.Status.Incarnation; incarnation != nil {
			pod = pods[resource.Namespace+"/"+incarnation.Name]
		}
		view, err := sandboxes.View(resource, pod)
		if err != nil {
			return nil, watchHealthView{}, err
		}
		views = append(views, sandboxView(view))
	}
	sort.Slice(views, func(i, j int) bool { return views[i].Name < views[j].Name })
	return views, healthView(snapshot.Watch), nil
}

func snapshotSandbox(snapshot liveinventory.Snapshot, name string) (*sandboxapi.Sandbox, *corev1.Pod) {
	var resource *sandboxapi.Sandbox
	for i := range snapshot.Sandboxes {
		if snapshot.Sandboxes[i].Name == name {
			resource = &snapshot.Sandboxes[i]
			break
		}
	}
	if resource == nil {
		return nil, nil
	}
	incarnation := resource.Status.Incarnation
	if incarnation == nil {
		return resource, nil
	}
	for i := range snapshot.Pods {
		pod := &snapshot.Pods[i]
		if pod.Namespace == resource.Namespace && pod.Name == incarnation.Name && string(pod.UID) == incarnation.UID {
			return resource, pod
		}
	}
	return resource, nil
}

func healthView(health watchhealth.Status) watchHealthView {
	failing := make(map[string]kindFailureView, len(health.Failing))
	for kind, failure := range health.Failing {
		failing[string(kind)] = kindFailureView{Error: failure.Error, Since: failure.Since}
	}
	return watchHealthView{Healthy: health.Available, Failing: failing}
}

func nonNilBindingViews(values []policyaccess.BindingView) []policyaccess.BindingView {
	if values == nil {
		return []policyaccess.BindingView{}
	}
	return values
}

func (r *streamingRoutes) streamSnapshots(w http.ResponseWriter, request *http.Request, ctx context.Context, liveChanges <-chan struct{}, snapshot func(context.Context) (any, error), health func() any, extra ...<-chan struct{}) {
	if err := startSSE(w); err != nil {
		return
	}
	value, err := snapshot(ctx)
	if err != nil {
		r.logger().WarnContext(ctx, "live stream snapshot failed", "error", err)
		return
	}
	if writeSSE(w, "snapshot", value, nil) != nil {
		return
	}
	timer := time.NewTimer(liveHealthInterval)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-timer.C:
			value := health()
			if value != nil && writeSSE(w, "health", value, nil) != nil {
				return
			}
			timer.Reset(liveHealthInterval)
		case <-liveChanges:
			timer.Reset(liveHealthInterval)
			value, err := snapshot(ctx)
			if err != nil {
				r.logger().WarnContext(ctx, "live stream snapshot failed", "error", err)
				return
			}
			if writeSSE(w, "snapshot", value, nil) != nil {
				return
			}
		case <-firstSignal(extra):
			timer.Reset(liveHealthInterval)
			value, err := snapshot(ctx)
			if err != nil {
				r.logger().WarnContext(ctx, "live stream snapshot failed", "error", err)
				return
			}
			if writeSSE(w, "snapshot", value, nil) != nil {
				return
			}
		}
	}
}

func firstSignal(channels []<-chan struct{}) <-chan struct{} {
	for _, channel := range channels {
		if channel != nil {
			return channel
		}
	}
	return nil
}

func (r *streamingRoutes) requestLifetime(request *http.Request) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(request.Context())
	drain := r.server.DrainDone()
	go func() {
		select {
		case <-drain:
			cancel()
		case <-ctx.Done():
		}
	}()
	return ctx, cancel
}

func (r *streamingRoutes) operatorLifetime(request *http.Request) (context.Context, context.CancelFunc, error) {
	ctx, cancel := r.requestLifetime(request)
	session, ok := auth.SessionFromContext(request.Context())
	if !ok || session.Login == nil || session.Row == nil || r.deps.Auth == nil {
		cancel()
		return nil, nil, errors.New("operator session is unavailable")
	}
	go func() {
		if err := r.deps.Auth.UntilEnded(ctx, session.Row.ID()); err == nil {
			cancel()
		} else if ctx.Err() == nil {
			r.logger().WarnContext(ctx, "operator session watch failed", "error", err)
		}
	}()
	return ctx, cancel, nil
}

func (r *streamingRoutes) dispatcher(w http.ResponseWriter, request *http.Request) {
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	if r.deps.DispatcherHTTP == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Dispatcher is not configured")
		return
	}
	r.forwardDispatcher(w, request, ctx)
}

func (r *streamingRoutes) forwardDispatcher(w http.ResponseWriter, request *http.Request, ctx context.Context) {
	if err := r.deps.DispatcherHTTP.ForwardHTTP(w, request, ctx, request.PathValue("path"), ""); err != nil {
		writeUpstreamOpenError(w, http.MethodGet, request, err)
	}
}

func (r *streamingRoutes) actionFacade(w http.ResponseWriter, request *http.Request) {
	r.operatorFacade(w, request, r.deps.ActionHTTP, "Action facade")
}

func (r *streamingRoutes) connectionFacade(w http.ResponseWriter, request *http.Request) {
	r.operatorFacade(w, request, r.deps.ConnectionHTTP, "Connection facade")
}

func (r *streamingRoutes) operatorFacade(w http.ResponseWriter, request *http.Request, upstream streamingUpstream, label string) {
	path := request.PathValue("path")
	if !proxy.GeneratedFacadeRouteAllowed(request.Method, path) {
		writeDetailError(w, http.StatusNotFound, "unknown "+label+" route")
		return
	}
	if upstream == nil {
		writeDetailError(w, http.StatusServiceUnavailable, label+" is not configured")
		return
	}
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	caller, _ := auth.CallerFromContext(request.Context())
	session, _ := auth.SessionFromContext(request.Context())
	if caller.Kind != auth.CallerOperator || session == nil || session.Row == nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	if r.deps.Federator == nil {
		writeDetailError(w, http.StatusServiceUnavailable, `{"code":"operator_federation_not_configured"}`)
		return
	}
	bearer, err := r.deps.Federator.Exchange(ctx, session.Row)
	if err != nil {
		writeFederationError(w, err)
		return
	}
	r.forwardFacade(w, request, ctx, upstream, path, bearer)
}

func (r *streamingRoutes) forwardFacade(w http.ResponseWriter, request *http.Request, ctx context.Context, upstream streamingUpstream, path, bearer string) {
	if err := upstream.ForwardHTTP(w, request, ctx, path, bearer); err != nil {
		writeUpstreamOpenError(w, request.Method, request, err)
	}
}

func writeFederationError(w http.ResponseWriter, err error) {
	var federationError *federation.FederationError
	if errors.As(err, &federationError) {
		writeJSONResponse(w, federationError.Status, map[string]any{"detail": map[string]string{"code": federationError.Code}})
		return
	}
	writeJSONResponse(w, http.StatusBadGateway, map[string]any{"detail": map[string]any{"method": http.MethodPost, "url": "", "upstream_status": nil, "error_type": fmt.Sprintf("%T", err)}})
}

func writeUpstreamOpenError(w http.ResponseWriter, method string, request *http.Request, err error) {
	code := http.StatusServiceUnavailable
	if errors.Is(err, context.DeadlineExceeded) {
		code = http.StatusGatewayTimeout
	}
	path := request.URL.Path
	detail := map[string]any{"method": method, "url": path, "upstream_status": nil, "error_type": fmt.Sprintf("%T", err)}
	writeJSONResponse(w, code, map[string]any{"detail": detail})
}

func writeJSONResponse(w http.ResponseWriter, statusCode int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(statusCode)
	_ = json.NewEncoder(w).Encode(value)
}

func (r *streamingRoutes) sandboxNotifications(w http.ResponseWriter, request *http.Request) {
	if r.deps.Sandboxes == nil || r.deps.Notifications == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Notification Service is not configured")
		return
	}
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	sandbox, err := r.deps.Sandboxes.Get(ctx, request.PathValue("name"))
	if err != nil {
		_ = sandboxHTTPErrorToRaw(w, err)
		return
	}
	stream, err := r.deps.Notifications.WatchSandboxNotifications(ctx, &notifications_operatorpb.WatchSandboxNotificationsRequest{Namespace: sandbox.GetNamespace(), Name: sandbox.GetName(), Uid: sandbox.GetUid()})
	if err != nil {
		writeGRPCOpenError(w, err)
		return
	}
	streamCtx, stop := context.WithCancel(ctx)
	defer stop()
	// The RPC was opened with ctx so cancellation reaches transport; the route's relay still
	// watches its own lifetime and the first snapshot bound separately below.
	r.relayNotificationStream(w, streamCtx, "Notification", func() (proto.Message, error) { return stream.Recv() }, func(message proto.Message) (proto.Message, bool) {
		response, ok := message.(*notifications_operatorpb.WatchSandboxNotificationsResponse)
		if !ok || response == nil {
			return nil, false
		}
		if response.GetReconnectRequired() != nil {
			return nil, true
		}
		return response.GetSnapshot(), false
	}, r.deps.NotificationWait)
}

func (r *streamingRoutes) threadNotices(w http.ResponseWriter, request *http.Request) {
	if r.deps.Notifications == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Notification Service is not configured")
		return
	}
	values, present := request.URL.Query()["thread_id"]
	if !present || len(values) == 0 || len(values) > maxNoticeThreads {
		writeDetailError(w, http.StatusUnprocessableEntity, "thread_id must contain between 1 and 200 values")
		return
	}
	ids := make([]string, 0, len(values))
	for _, value := range values {
		id, err := uuid.Parse(value)
		if err != nil {
			writeDetailError(w, http.StatusUnprocessableEntity, "thread_id must be a UUID")
			return
		}
		ids = append(ids, id.String())
	}
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	stream, err := r.deps.Notifications.WatchSessionNotices(ctx, &notifications_operatorpb.WatchSessionNoticesRequest{SessionIds: ids})
	if err != nil {
		writeGRPCOpenError(w, err)
		return
	}
	streamCtx, stop := context.WithCancel(ctx)
	defer stop()
	r.relayNotificationStream(w, streamCtx, "Notification", func() (proto.Message, error) { return stream.Recv() }, func(message proto.Message) (proto.Message, bool) {
		response, ok := message.(*notifications_operatorpb.WatchSessionNoticesResponse)
		if !ok || response == nil {
			return nil, false
		}
		if response.GetReconnectRequired() != nil {
			return nil, true
		}
		return response.GetSnapshot(), false
	}, r.deps.NotificationWait)
}

func (r *streamingRoutes) relayNotificationStream(w http.ResponseWriter, ctx context.Context, label string, recv func() (proto.Message, error), snapshot func(proto.Message) (proto.Message, bool), firstWait time.Duration) {
	if startSSE(w) != nil {
		return
	}
	if firstWait <= 0 {
		firstWait = 5 * time.Second
	}
	firstTimer := time.NewTimer(firstWait)
	defer firstTimer.Stop()
	first := true
	for {
		result := make(chan struct {
			message proto.Message
			err     error
		}, 1)
		go func() {
			message, err := recv()
			result <- struct {
				message proto.Message
				err     error
			}{message, err}
		}()
		select {
		case <-ctx.Done():
			return
		case <-firstTimer.C:
			if first {
				r.logger().WarnContext(ctx, label+" stream did not send an initial snapshot before its deadline")
				return
			}
		case received := <-result:
			if received.err != nil {
				if ctx.Err() == nil && !errors.Is(received.err, io.EOF) {
					r.logger().WarnContext(ctx, label+" stream interrupted", "error", received.err)
				}
				return
			}
			message, reconnect := snapshot(received.message)
			if reconnect {
				return
			}
			if message == nil {
				r.logger().WarnContext(ctx, label+" stream sent an empty snapshot")
				return
			}
			data, err := (protojson.MarshalOptions{EmitDefaultValues: true}).Marshal(message)
			if err != nil || writeSSEData(w, "snapshot", data, nil) != nil {
				return
			}
			first = false
			if !firstTimer.Stop() {
				select {
				case <-firstTimer.C:
				default:
				}
			}
		}
	}
}

func (r *streamingRoutes) console(w http.ResponseWriter, request *http.Request) {
	if r.deps.Sandboxes == nil || r.deps.Console == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Sandbox console is unavailable")
		return
	}
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	sandbox, err := r.deps.Sandboxes.Get(ctx, request.PathValue("name"))
	if err != nil {
		_ = sandboxHTTPErrorToRaw(w, err)
		return
	}
	if err := startSSE(w); err != nil {
		return
	}
	console, err := r.deps.Console.Open(ctx, sandbox.GetNamespace(), sandbox.GetName())
	if err != nil {
		var unavailable *sandboxes.ConsoleUnavailableError
		if errors.As(err, &unavailable) {
			_ = writeSSE(w, "unavailable", err.Error(), nil)
		} else {
			r.logger().WarnContext(ctx, "Sandbox console stream failed", "error", err)
		}
		return
	}
	r.relayConsole(w, ctx, console)
}

func (r *streamingRoutes) relayConsole(w http.ResponseWriter, ctx context.Context, console io.ReadCloser) {
	defer console.Close()
	buffer := make([]byte, 32*1024)
	for {
		read := make(chan struct {
			count int
			err   error
		}, 1)
		go func() {
			count, err := console.Read(buffer)
			read <- struct {
				count int
				err   error
			}{count, err}
		}()
		select {
		case <-ctx.Done():
			return
		case result := <-read:
			if result.count > 0 && writeSSE(w, "output", base64.StdEncoding.EncodeToString(buffer[:result.count]), nil) != nil {
				return
			}
			if result.err != nil {
				if result.err != io.EOF && ctx.Err() == nil {
					var unavailable *sandboxes.ConsoleUnavailableError
					if errors.As(result.err, &unavailable) {
						_ = writeSSE(w, "unavailable", result.err.Error(), nil)
					} else {
						r.logger().WarnContext(ctx, "Sandbox console stream ended", "error", result.err)
					}
				}
				return
			}
		}
	}
}

func (r *streamingRoutes) threadEvents(w http.ResponseWriter, request *http.Request) {
	if r.deps.Threads == nil || r.deps.History == nil || r.deps.HistoryFollower == nil || r.deps.Updates == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Thread event stream is unavailable")
		return
	}
	id, err := uuid.Parse(request.PathValue("thread_id"))
	if err != nil {
		writeDetailError(w, http.StatusUnprocessableEntity, "thread_id must be a UUID")
		return
	}
	thread, err := r.deps.Threads.GetThread(request.Context(), id)
	if err != nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Thread metadata is unavailable")
		return
	}
	if thread == nil {
		writeDetailError(w, http.StatusNotFound, "no thread "+id.String())
		return
	}
	cursor := uint64(0)
	if text := request.URL.Query().Get("after"); text != "" {
		cursor, err = strconv.ParseUint(text, 10, 64)
		if err != nil {
			writeDetailError(w, http.StatusUnprocessableEntity, "after must be a non-negative integer")
			return
		}
	}
	if text := request.Header.Get("Last-Event-ID"); text != "" {
		cursor, err = strconv.ParseUint(text, 10, 64)
		if err != nil {
			writeDetailError(w, http.StatusUnprocessableEntity, "Last-Event-ID must be a non-negative integer")
			return
		}
	}
	if cursor > 0 {
		watermark, err := r.deps.History.ReadWatermark(request.Context(), id)
		if err != nil {
			writeDetailError(w, http.StatusServiceUnavailable, "Thread archive is unavailable")
			return
		}
		if cursor > watermark {
			writeDetailError(w, http.StatusConflict, "cursor is beyond the archived Thread prefix")
			return
		}
	}
	var awaiting *string
	if value, ok := request.URL.Query()["awaiting_command_id"]; ok && len(value) > 0 {
		awaiting = &value[0]
	}
	ctx, cancel := r.requestLifetime(request)
	defer cancel()
	changes, unsubscribe, err := r.deps.Updates.Subscribe(threads.UpdatesChannel)
	if err != nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Thread updates are unavailable")
		return
	}
	defer unsubscribe()
	if startSSE(w) != nil {
		return
	}
	attachedSent := false
	checkSummary := func() bool {
		summary, err := r.deps.History.GetSummary(ctx, id)
		if err != nil {
			r.logger().WarnContext(ctx, "read Thread event feed status", "error", err)
			return true
		}
		if !attachedSent && summary.Feed != nil && summary.Feed.Attached != nil {
			data, marshalErr := (protojson.MarshalOptions{}).Marshal(summary.Feed.Attached)
			if marshalErr != nil || writeSSEData(w, "attached", data, nil) != nil {
				return true
			}
			attachedSent = true
		}
		if awaiting == nil && summary.Feed != nil && summary.Feed.End != nil && summary.Feed.Attached != nil && summary.CommittedCursor <= cursor && summary.Feed.Attached.GetLastCursor() >= cursor {
			if summary.Feed.Status == historypb.FeedStatus_FEED_STATUS_FAILED {
				_ = writeSSE(w, "error", map[string]string{"message": summary.Feed.End.Message}, nil)
			} else {
				_ = writeSSE(w, "end", map[string]any{}, nil)
			}
			return true
		}
		return false
	}
	heartbeat := r.deps.ThreadHeartbeat
	if heartbeat <= 0 {
		heartbeat = 15 * time.Second
	}
	timer := time.NewTimer(heartbeat)
	defer timer.Stop()
	resetHeartbeat := func() {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(heartbeat)
	}
	waitForHistory := func() bool {
		select {
		case <-ctx.Done():
			return true
		case <-changes:
			resetHeartbeat()
			return checkSummary()
		case <-timer.C:
			if _, err := io.WriteString(w, ": keepalive\n\n"); err != nil {
				return true
			}
			if flusher, ok := w.(http.Flusher); ok {
				flusher.Flush()
			}
			if checkSummary() {
				return true
			}
			timer.Reset(heartbeat)
			return false
		}
	}
	if checkSummary() {
		return
	}
	type followOutcome int
	const (
		followStop followOutcome = iota
		followRenew
		followRetryMissing
	)
	followOnce := func() followOutcome {
		followCtx, cancelFollow := context.WithCancel(ctx)
		defer cancelFollow()
		stream, err := r.deps.HistoryFollower.FollowSession(followCtx, &historypb.FollowSessionRequest{
			SessionId: id.String(), Start: &historypb.FollowSessionRequest_AfterCursor{AfterCursor: cursor}, Follow: true,
		})
		if err != nil {
			if status.Code(err) == codes.NotFound {
				return followRetryMissing
			}
			r.logger().WarnContext(ctx, "open Thread event follow", "error", err)
			return followStop
		}
		received := make(chan struct {
			response *historypb.FollowSessionResponse
			err      error
		}, 1)
		startReceive := func() {
			go func() {
				response, err := stream.Recv()
				received <- struct {
					response *historypb.FollowSessionResponse
					err      error
				}{response: response, err: err}
			}()
		}
		startReceive()
		for {
			select {
			case <-ctx.Done():
				return followStop
			case <-changes:
				if checkSummary() {
					return followStop
				}
				resetHeartbeat()
			case <-timer.C:
				if _, err := io.WriteString(w, ": keepalive\n\n"); err != nil {
					return followStop
				}
				if flusher, ok := w.(http.Flusher); ok {
					flusher.Flush()
				}
				if checkSummary() {
					return followStop
				}
				timer.Reset(heartbeat)
			case result := <-received:
				if result.err != nil {
					if ctx.Err() != nil {
						return followStop
					}
					if status.Code(result.err) == codes.NotFound {
						return followRetryMissing
					}
					if errors.Is(result.err, io.EOF) {
						r.logger().WarnContext(ctx, "Thread event follow ended before a terminal frame", "error", result.err)
					} else {
						r.logger().WarnContext(ctx, "Thread event follow ended", "error", result.err)
					}
					return followStop
				}
				if result.response == nil {
					r.logger().WarnContext(ctx, "Thread event follow returned an empty frame")
					return followStop
				}
				response := result.response
				switch frame := response.GetFrame().(type) {
				case *historypb.FollowSessionResponse_Entries:
					if frame.Entries == nil {
						r.logger().WarnContext(ctx, "Thread event follow returned an empty entries frame")
						return followStop
					}
					for _, entry := range frame.Entries.GetEntries() {
						if entry == nil {
							r.logger().WarnContext(ctx, "Thread event follow returned an empty entry")
							return followStop
						}
						data, marshalErr := (protojson.MarshalOptions{}).Marshal(entry)
						entryCursor := entry.GetCursor()
						if marshalErr != nil || writeSSEData(w, "event", data, &entryCursor) != nil {
							return followStop
						}
						if awaiting != nil && entry.GetEvent() != nil && entry.GetEvent().GetCommandAdmitted() != nil && entry.GetEvent().GetCommandAdmitted().GetCommand().GetCommandId() == *awaiting {
							awaiting = nil
						}
					}
					cursor = response.GetThroughCursor()
				case *historypb.FollowSessionResponse_CaughtUp:
					cursor = response.GetThroughCursor()
					if checkSummary() {
						return followStop
					}
				case *historypb.FollowSessionResponse_Settled, *historypb.FollowSessionResponse_Removed:
				case *historypb.FollowSessionResponse_ReconnectRequired:
					cursor = response.GetThroughCursor()
					if checkSummary() {
						return followStop
					}
					return followRenew
				default:
					r.logger().WarnContext(ctx, "Thread event follow returned an unknown frame")
					return followStop
				}
				resetHeartbeat()
				startReceive()
			}
		}
	}
	for {
		switch followOnce() {
		case followRenew:
			continue
		case followRetryMissing:
			if waitForHistory() {
				return
			}
		default:
			return
		}
	}
}

func decisionsRequest(subject *sandboxespb.ServiceAccount) *egress_adminpb.WatchDecisionsRequest {
	return &egress_adminpb.WatchDecisionsRequest{Filter: &egress_adminpb.DecisionFilter{Scope: &egress_adminpb.DecisionFilter_Subject{Subject: subject}}}
}

func decisionViews(rows []*egress_adminpb.Decision) []decisionView {
	values := make([]decisionView, 0, len(rows))
	for _, row := range rows {
		if row == nil {
			continue
		}
		outcome := "deny"
		if row.GetOutcome() == egress_adminpb.Decision_OUTCOME_ALLOW {
			outcome = "allow"
		}
		view := decisionView{Method: row.GetMethod(), Host: row.GetHost(), Port: row.GetPort(), Outcome: outcome, Substituted: row.GetSubstituted()}
		if row.GetAt() != nil && row.GetAt().IsValid() {
			view.At = row.GetAt().AsTime().UTC()
		}
		if row.Reason != nil {
			value := row.GetReason()
			view.Reason = &value
		}
		if row.Binding != nil {
			value := row.GetBinding()
			view.Binding = &value
		}
		if row.Policy != nil {
			value := row.GetPolicy()
			view.Policy = &value
		}
		if row.Rule != nil {
			value := row.GetRule()
			view.Rule = &value
		}
		if row.Address != nil {
			value := row.GetAddress()
			view.Address = &value
		}
		values = append(values, view)
	}
	return values
}

func grpcFailure(err error) string {
	if err == nil {
		return "Unknown"
	}
	converted := status.Convert(err)
	return converted.Code().String() + ": " + converted.Message()
}

func writeGRPCOpenError(w http.ResponseWriter, err error) {
	code := http.StatusServiceUnavailable
	if status.Code(err) == codes.DeadlineExceeded {
		code = http.StatusGatewayTimeout
	}
	writeDetailError(w, code, grpcFailure(err))
}

func sandboxHTTPErrorToRaw(w http.ResponseWriter, err error) error {
	var notFound *sandboxes.SandboxNotFoundError
	var invalid *sandboxes.InvalidSandboxError
	var conflict *sandboxes.SandboxConflictError
	switch {
	case errors.As(err, &notFound):
		writeDetailError(w, http.StatusNotFound, err.Error())
	case errors.As(err, &invalid):
		writeDetailError(w, http.StatusUnprocessableEntity, err.Error())
	case errors.As(err, &conflict):
		writeDetailError(w, http.StatusConflict, err.Error())
	default:
		writeDetailError(w, http.StatusServiceUnavailable, "Sandbox inventory is unavailable")
	}
	return err
}

func parseOptionalBool(values url.Values, name string, fallback bool) (bool, error) {
	raw, ok := values[name]
	if !ok {
		return fallback, nil
	}
	if len(raw) != 1 {
		return false, errors.New("expected one boolean value")
	}
	return strconv.ParseBool(raw[0])
}

func startSSE(w http.ResponseWriter) error {
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
	return nil
}

func writeSSE(w http.ResponseWriter, event string, value any, id *uint64) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return writeSSEData(w, event, data, id)
}

func writeSSEData(w http.ResponseWriter, event string, data []byte, id *uint64) error {
	if _, err := fmt.Fprintf(w, "event: %s\n", event); err != nil {
		return err
	}
	if id != nil {
		if _, err := fmt.Fprintf(w, "id: %d\n", *id); err != nil {
			return err
		}
	}
	if _, err := fmt.Fprintf(w, "data: %s\n\n", data); err != nil {
		return err
	}
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
	return nil
}

func waitContext(ctx context.Context, duration time.Duration) bool {
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-timer.C:
		return true
	}
}

func compactDuration(duration time.Duration) string {
	if duration <= 0 {
		duration = 5 * time.Second
	}
	seconds := duration.Seconds()
	return strconv.FormatFloat(seconds, 'f', -1, 64) + "s"
}
