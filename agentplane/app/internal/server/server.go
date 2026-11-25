package server

import (
	"net/http"
	"regexp"
	"sync"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	"github.com/agentydragon/ducktape/agentplane/app/internal/httpresponse"
)

// httpServer owns the standard-library router and the readiness drain gate. A single mux keeps
// auth and health routes on one listener.
type httpServer struct {
	mux            *uuidMux
	authentication *auth.Auth
	drain          atomic.Bool
	drainDone      chan struct{}
	drainOnce      sync.Once
}

func newHTTPServer(authentication *auth.Auth) *httpServer {
	mux := &uuidMux{ServeMux: http.NewServeMux()}
	server := &httpServer{mux: mux, authentication: authentication, drainDone: make(chan struct{})}
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, _ *http.Request) {
		if server.drain.Load() {
			http.Error(w, "shutting down", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusNoContent)
	})
	if authentication != nil {
		authentication.RegisterRoutes(mux.ServeMux)
	}
	return server
}

// ServeMux wildcards have no type constraints. Apply the UUID route constraint before auth, so a
// non-UUID thread or session ID matches no route.
type uuidMux struct{ *http.ServeMux }

var uuidPathSegment = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

func (m *uuidMux) Handle(pattern string, handler http.Handler) {
	m.ServeMux.Handle(pattern, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		for _, name := range []string{"thread_id", "session_id"} {
			if value := r.PathValue(name); value != "" && !uuidPathSegment.MatchString(value) {
				http.NotFound(w, r)
				return
			}
		}
		handler.ServeHTTP(w, r)
	}))
}

func (m *uuidMux) HandleFunc(pattern string, handler func(http.ResponseWriter, *http.Request)) {
	m.Handle(pattern, http.HandlerFunc(handler))
}

// RegisterRawRoute mounts an endpoint on the app's mux. Caller routes use the app's OIDC
// cookie-or-ID-token boundary; operator routes additionally require a browser login.
func (s *httpServer) RegisterRawRoute(pattern string, handler http.Handler, operatorOnly bool) {
	if s.authentication == nil {
		handler = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			httpresponse.WriteDetailError(w, http.StatusUnauthorized, "no session and no accepted token")
		})
	} else {
		if operatorOnly {
			next := handler
			handler = http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
				caller, ok := auth.CallerFromContext(request.Context())
				session, hasSession := auth.SessionFromContext(request.Context())
				if !ok || caller.Kind != auth.CallerOperator || !hasSession || session.Login == nil {
					httpresponse.WriteDetailError(w, http.StatusForbidden, "operator login required")
					return
				}
				next.ServeHTTP(w, request)
			})
		}
		handler = s.authentication.RequireCaller(handler)
	}
	s.mux.Handle(pattern, handler)
}

func (s *httpServer) BeginDrain() {
	s.drainOnce.Do(func() {
		s.drain.Store(true)
		close(s.drainDone)
	})
}

// DrainDone closes as soon as graceful shutdown begins so long-lived streams can end promptly.
func (s *httpServer) DrainDone() <-chan struct{} { return s.drainDone }

func (s *httpServer) Ready() bool { return !s.drain.Load() }

// Handler installs the session middleware outside the router, so session changes commit before
// SSE headers.
func (s *httpServer) Handler(authentication *auth.Auth) http.Handler {
	if authentication == nil {
		return s.mux
	}
	return authentication.Middleware(s.mux)
}
