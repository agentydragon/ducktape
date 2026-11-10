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
	"strconv"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/agentydragon/ducktape/agentplane/app/go/federation"
	"github.com/agentydragon/ducktape/agentplane/app/go/proxy"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/agentydragon/ducktape/agentplane/app/go/threads"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

type streamingUpstream interface {
	ForwardHTTP(http.ResponseWriter, *http.Request, context.Context, string, string) error
}

type streamSandboxInventory interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

type streamHistoryStore interface {
	threadReadHistory
	ReadWatermark(context.Context, uuid.UUID) (uint64, error)
}

// StreamingDependencies are the narrow runtime interfaces used by SSE and raw upstream routes.
// All route authorization still goes through HTTPServer.RegisterRawRoute.
type actionFederator interface {
	Exchange(context.Context, federation.SessionLoginRow) (string, error)
}

type StreamingDependencies struct {
	Auth             *auth.Auth
	Updates          auth.ChangeSource
	UpdatesConnected func() bool
	SandboxNamespace string
	Sandboxes        streamSandboxInventory
	Threads          threadReadStore
	History          streamHistoryStore
	HistoryFollower  interface {
		FollowSession(context.Context, *historypb.FollowSessionRequest) (grpc.ServerStreamingClient[historypb.FollowSessionResponse], error)
	}
	Federator        actionFederator
	DispatcherHTTP   streamingUpstream
	EnvironmentHTTP  streamingUpstream
	PolicyHTTP       streamingUpstream
	EgressHTTP       streamingUpstream
	ActionHTTP       streamingUpstream
	ConnectionHTTP   streamingUpstream
	HistoryHTTP      streamingUpstream
	NotificationHTTP streamingUpstream
	ThreadHeartbeat  time.Duration
	Console          sandboxes.ConsoleSource
	Logger           *slog.Logger
}

// RegisterStreamingRoutes installs the live snapshots, event/notice/console streams, and
// operator-facing HTTP facades. The server owns the shared auth/session middleware and drain gate.
func RegisterStreamingRoutes(server *HTTPServer, dependencies StreamingDependencies) {
	routes := newStreamingRoutes(server, dependencies)
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodPatch, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/environments/{path...}", http.HandlerFunc(routes.environmentFacade), false)
	}
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/policy/{path...}", http.HandlerFunc(routes.policyFacade), true)
	}
	server.RegisterRawRoute("GET /upstream/egress/{path...}", http.HandlerFunc(routes.egressFacade), true)
	server.RegisterRawRoute("GET /threads/{thread_id}/events/stream", http.HandlerFunc(routes.threadEvents), false)
	server.RegisterRawRoute("GET /upstream/notifications/{path...}", http.HandlerFunc(routes.notificationFacade), true)
	server.RegisterRawRoute("GET /sandboxes/{name}/console/stream", http.HandlerFunc(routes.console), true)
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		server.RegisterRawRoute(method+" /upstream/dispatcher/{path...}", http.HandlerFunc(routes.dispatcher), true)
	}
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodPatch, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/actions/{path...}", http.HandlerFunc(routes.actionFacade), true)
		server.RegisterRawRoute(method+" /upstream/connections/{path...}", http.HandlerFunc(routes.connectionFacade), true)
	}
	for _, method := range []string{http.MethodGet, http.MethodPost} {
		server.RegisterRawRoute(method+" /upstream/history/{path...}", http.HandlerFunc(routes.historyFacade), false)
	}
}

type streamingRoutes struct {
	server *HTTPServer
	deps   StreamingDependencies
}

func newStreamingRoutes(server *HTTPServer, deps StreamingDependencies) *streamingRoutes {
	return &streamingRoutes{server: server, deps: deps}
}

func (r *streamingRoutes) logger() *slog.Logger {
	if r.deps.Logger != nil {
		return r.deps.Logger
	}
	return slog.Default()
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

// environmentFacade forwards the generated EnvironmentService API with the app's Pod-bound
// identity. Browser sessions stay tied to logout; bearer callers stay tied to the request.
func (r *streamingRoutes) environmentFacade(w http.ResponseWriter, request *http.Request) {
	path := request.PathValue("path")
	if !proxy.GeneratedFacadeRouteAllowed(request.Method, path) {
		writeDetailError(w, http.StatusNotFound, "unknown Environment Service facade route")
		return
	}
	if r.deps.EnvironmentHTTP == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Environment Service is not configured")
		return
	}
	var ctx context.Context
	var cancel context.CancelFunc
	if session, ok := auth.SessionFromContext(request.Context()); ok && session.Login != nil {
		var err error
		ctx, cancel, err = r.operatorLifetime(request)
		if err != nil {
			writeDetailError(w, http.StatusForbidden, "operator login required")
			return
		}
	} else {
		ctx, cancel = r.requestLifetime(request)
	}
	defer cancel()
	r.forwardFacade(w, request, ctx, r.deps.EnvironmentHTTP, path, "")
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
		writeUpstreamOpenError(w, request.Method, request, err)
	}
}

func (r *streamingRoutes) actionFacade(w http.ResponseWriter, request *http.Request) {
	r.operatorFacade(w, request, r.deps.ActionHTTP, "Action facade")
}

// These backends admit the app's service account; the browser session bounds each stream.
func (r *streamingRoutes) policyFacade(w http.ResponseWriter, request *http.Request) {
	r.serviceAccountFacade(w, request, r.deps.PolicyHTTP, "Policy Service")
}

func (r *streamingRoutes) egressFacade(w http.ResponseWriter, request *http.Request) {
	r.serviceAccountFacade(w, request, r.deps.EgressHTTP, "Egress Service")
}

func (r *streamingRoutes) serviceAccountFacade(w http.ResponseWriter, request *http.Request, upstream streamingUpstream, label string) {
	path := request.PathValue("path")
	if !proxy.GeneratedFacadeRouteAllowed(request.Method, path) {
		writeDetailError(w, http.StatusNotFound, "unknown "+label+" facade route")
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
	r.forwardFacade(w, request, ctx, upstream, path, "")
}

func (r *streamingRoutes) connectionFacade(w http.ResponseWriter, request *http.Request) {
	r.operatorFacade(w, request, r.deps.ConnectionHTTP, "Connection facade")
}

func (r *streamingRoutes) historyFacade(w http.ResponseWriter, request *http.Request) {
	path := request.PathValue("path")
	if !proxy.GeneratedFacadeRouteAllowed(request.Method, path) {
		writeDetailError(w, http.StatusNotFound, "unknown History facade route")
		return
	}
	if r.deps.HistoryHTTP == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "History facade is not configured")
		return
	}
	r.forwardFacade(w, request, request.Context(), r.deps.HistoryHTTP, path, "")
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

func (r *streamingRoutes) notificationFacade(w http.ResponseWriter, request *http.Request) {
	r.serviceAccountFacade(w, request, r.deps.NotificationHTTP, "Notification Service")
}

func (r *streamingRoutes) console(w http.ResponseWriter, request *http.Request) {
	if r.deps.Sandboxes == nil || r.deps.Console == nil {
		writeDetailError(w, http.StatusServiceUnavailable, "Sandbox console is unavailable")
		return
	}
	uid := request.URL.Query().Get("sandbox_uid")
	if uid == "" {
		writeDetailError(w, http.StatusBadRequest, "sandbox_uid is required")
		return
	}
	ctx, cancel, err := r.operatorLifetime(request)
	if err != nil {
		writeDetailError(w, http.StatusForbidden, "operator login required")
		return
	}
	defer cancel()
	target := &identitypb.EnvironmentTarget{Namespace: r.deps.SandboxNamespace, Name: request.PathValue("name"), Uid: uid}
	sandbox, err := r.deps.Sandboxes.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target})
	if err != nil {
		_ = sandboxHTTPErrorToRaw(w, err)
		return
	}
	if sandbox == nil || !proto.Equal(sandbox.GetTarget(), target) {
		writeDetailError(w, http.StatusServiceUnavailable, "Environment identity is incomplete or mismatched")
		return
	}
	if err := startSSE(w); err != nil {
		return
	}
	console, err := r.deps.Console.Open(ctx, target.GetNamespace(), target.GetName())
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
		attached := summary.GetAttached()
		if !attachedSent && attached != nil {
			data, marshalErr := (protojson.MarshalOptions{}).Marshal(attached)
			if marshalErr != nil || writeSSEData(w, "attached", data, nil) != nil {
				return true
			}
			attachedSent = true
		}
		terminal := summary.GetFeedStatus() == historypb.FeedStatus_FEED_STATUS_ENDED || summary.GetFeedStatus() == historypb.FeedStatus_FEED_STATUS_FAILED
		if awaiting == nil && terminal && attached != nil && summary.GetCommittedCursor() <= cursor && attached.GetLastCursor() >= cursor {
			if summary.GetFeedStatus() == historypb.FeedStatus_FEED_STATUS_FAILED {
				_ = writeSSE(w, "error", map[string]string{"message": summary.GetFeedError()}, nil)
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
	switch {
	case status.Code(err) == codes.NotFound:
		writeDetailError(w, http.StatusNotFound, err.Error())
	default:
		writeDetailError(w, http.StatusServiceUnavailable, "Sandbox inventory is unavailable")
	}
	return err
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
