package main

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	"github.com/danielgtaylor/huma/v2"
	"github.com/danielgtaylor/huma/v2/adapters/humago"
)

// HTTPServer owns the standard-library router, Huma API and the readiness drain gate.
// A single mux keeps API documentation, auth and health routes on one listener.
type HTTPServer struct {
	mux            *http.ServeMux
	api            huma.API
	authentication *auth.Auth
	drain          atomic.Bool
	drainDone      chan struct{}
	drainOnce      sync.Once
}

func NewHTTPServer(authentication *auth.Auth) *HTTPServer {
	mux := http.NewServeMux()
	apiConfig := huma.DefaultConfig("Agentplane", "0")
	// Keep Huma's generated OpenAPI document enabled; the custom docs pages below render it.
	apiConfig.DocsPath = ""
	// Huma adds optional $schema fields to object responses by default. Python's API models reject
	// unknown fields, so keep those response bodies on the existing wire contract while retaining
	// the generated OpenAPI document.
	apiConfig.CreateHooks = nil
	api := humago.New(mux, apiConfig)
	server := &HTTPServer{mux: mux, api: api, authentication: authentication, drainDone: make(chan struct{})}
	mux.HandleFunc("GET /docs", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(swaggerDocsHTML))
	})
	mux.HandleFunc("GET /redoc", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(redocDocsHTML))
	})
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
		authentication.RegisterRoutes(mux)
	}
	return server
}

const swaggerDocsHTML = `<!doctype html>
<html><head><title>Agentplane API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"></head>
<body><div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
<script>SwaggerUIBundle({url:"/openapi.json",dom_id:"#swagger-ui"})</script></body></html>`

const redocDocsHTML = `<!doctype html>
<html><head><title>Agentplane API</title></head>
<body><redoc spec-url="/openapi.json"></redoc>
<script src="https://cdn.jsdelivr.net/npm/redoc@2/bundles/redoc.standalone.js"></script></body></html>`

// API is the Huma registration point used by feature packages and the OpenAPI exporter.
func (s *HTTPServer) API() huma.API { return s.api }

// ProtectedAPI groups the application routes that require either an operator session or an
// audience-checked Kubernetes token, matching the Python app's router-wide caller dependency.
func (s *HTTPServer) ProtectedAPI() *huma.Group {
	group := huma.NewGroup(s.api)
	group.UseMiddleware(func(ctx huma.Context, next func(huma.Context)) {
		if s.authentication == nil {
			_, writer := humago.Unwrap(ctx)
			writeDetailError(writer, http.StatusUnauthorized, "no session and no accepted token")
			return
		}
		request, writer := humago.Unwrap(ctx)
		s.authentication.RequireCaller(http.HandlerFunc(func(_ http.ResponseWriter, request *http.Request) {
			if withContext, ok := ctx.(interface {
				WithContext(context.Context) huma.Context
			}); ok {
				next(withContext.WithContext(request.Context()))
				return
			}
			next(ctx)
		})).ServeHTTP(writer, request)
	})
	return group
}

// OperatorAPI groups endpoints that additionally require the browser OIDC login, as opposed to
// workload-token caller authentication.
func (s *HTTPServer) OperatorAPI() *huma.Group {
	group := huma.NewGroup(s.ProtectedAPI())
	group.UseMiddleware(func(ctx huma.Context, next func(huma.Context)) {
		request, writer := humago.Unwrap(ctx)
		session, ok := auth.SessionFromContext(request.Context())
		if !ok || session.Login == nil {
			writeDetailError(writer, http.StatusForbidden, "operator login required")
			return
		}
		next(ctx)
	})
	return group
}

// RegisterRawRoute mounts a non-JSON or streaming endpoint on the same mux as Huma. Caller routes
// use the app's cookie-or-TokenReview boundary; operator routes additionally require an OIDC login.
func (s *HTTPServer) RegisterRawRoute(pattern string, handler http.Handler, operatorOnly bool) {
	if s.authentication == nil {
		handler = http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
			writeDetailError(w, http.StatusUnauthorized, "no session and no accepted token")
		})
	} else {
		if operatorOnly {
			next := handler
			handler = http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
				caller, ok := auth.CallerFromContext(request.Context())
				session, hasSession := auth.SessionFromContext(request.Context())
				if !ok || caller.Kind != auth.CallerOperator || !hasSession || session.Login == nil {
					writeDetailError(w, http.StatusForbidden, "operator login required")
					return
				}
				next.ServeHTTP(w, request)
			})
		}
		handler = s.authentication.RequireCaller(handler)
	}
	s.mux.Handle(pattern, handler)
}

func writeDetailError(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": message})
}

func (s *HTTPServer) BeginDrain() {
	s.drainOnce.Do(func() {
		s.drain.Store(true)
		close(s.drainDone)
	})
}

// DrainDone closes as soon as graceful shutdown begins so long-lived streams can end promptly.
func (s *HTTPServer) DrainDone() <-chan struct{} { return s.drainDone }

func (s *HTTPServer) Ready() bool { return !s.drain.Load() }

// Handler installs the session middleware outside the API router, matching the Python app's
// request-wide browser-session behavior and ensuring session changes commit before SSE headers.
func (s *HTTPServer) Handler(authentication *auth.Auth) http.Handler {
	if authentication == nil {
		return s.mux
	}
	return authentication.Middleware(s.mux)
}
