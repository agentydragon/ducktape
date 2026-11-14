package server

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	"github.com/agentydragon/ducktape/agentplane/app/internal/federation"
	"github.com/agentydragon/ducktape/agentplane/app/internal/sandboxes"
	upstreamproxy "github.com/agentydragon/ducktape/agentplane/app/internal/upstream"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

type streamingUpstream interface {
	ForwardHTTP(http.ResponseWriter, *http.Request, context.Context, string, string) error
}

type streamSandboxInventory interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
}

type actionFederator interface {
	Exchange(context.Context, federation.SessionLoginRow) (string, error)
}

// streamingDependencies are the runtime interfaces for the raw facades and console stream.
// All route authorization goes through httpServer.RegisterRawRoute.
type streamingDependencies struct {
	Auth             *auth.Auth
	SandboxNamespace string
	Sandboxes        streamSandboxInventory
	Federator        actionFederator
	DispatcherHTTP   streamingUpstream
	EnvironmentHTTP  streamingUpstream
	PolicyHTTP       streamingUpstream
	EgressHTTP       streamingUpstream
	ActionHTTP       streamingUpstream
	ConnectionHTTP   streamingUpstream
	HistoryHTTP      streamingUpstream
	NotificationHTTP streamingUpstream
	Console          sandboxes.ConsoleSource
	Logger           *slog.Logger
}

// registerStreamingRoutes installs raw upstream facades and the console stream.
// The server owns the shared auth/session middleware and drain gate.
func registerStreamingRoutes(server *httpServer, dependencies streamingDependencies) {
	routes := newStreamingRoutes(server, dependencies)
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodPatch, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/environments/{path...}", http.HandlerFunc(routes.environmentFacade), false)
	}
	for _, method := range []string{http.MethodGet, http.MethodPost, http.MethodDelete} {
		server.RegisterRawRoute(method+" /upstream/policy/{path...}", http.HandlerFunc(routes.policyFacade), true)
	}
	server.RegisterRawRoute("GET /upstream/egress/{path...}", http.HandlerFunc(routes.egressFacade), true)
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
	server *httpServer
	deps   streamingDependencies
}

func newStreamingRoutes(server *httpServer, deps streamingDependencies) *streamingRoutes {
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
	if !upstreamproxy.GeneratedFacadeRouteAllowed(request.Method, path) {
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
	if !upstreamproxy.GeneratedFacadeRouteAllowed(request.Method, path) {
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
	if !upstreamproxy.GeneratedFacadeRouteAllowed(request.Method, path) {
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
	if !upstreamproxy.GeneratedFacadeRouteAllowed(request.Method, path) {
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
			_ = writeSSE(w, "unavailable", err.Error())
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
			if result.count > 0 && writeSSE(w, "output", base64.StdEncoding.EncodeToString(buffer[:result.count])) != nil {
				return
			}
			if result.err != nil {
				if result.err != io.EOF && ctx.Err() == nil {
					var unavailable *sandboxes.ConsoleUnavailableError
					if errors.As(result.err, &unavailable) {
						_ = writeSSE(w, "unavailable", result.err.Error())
					} else {
						r.logger().WarnContext(ctx, "Sandbox console stream ended", "error", result.err)
					}
				}
				return
			}
		}
	}
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

func writeSSE(w http.ResponseWriter, event string, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "event: %s\n", event); err != nil {
		return err
	}
	if _, err := fmt.Fprintf(w, "data: %s\n\n", data); err != nil {
		return err
	}
	if flusher, ok := w.(http.Flusher); ok {
		flusher.Flush()
	}
	return nil
}
