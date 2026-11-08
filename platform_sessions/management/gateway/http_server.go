package gateway

import (
	"context"
	"encoding/json"
	"net/http"
)

// HTTPConfig supplies gateway configuration and the generated owner UI.
type HTTPConfig struct {
	Owner              OwnerConfig
	Providers          map[string]ProviderHTTPConfig
	Bearers            []Bearer
	Assets             map[string][]byte
	ManagementDocument json.RawMessage
}

// HTTPServer implements browser login, owner management and the data plane on
// one HTTP server. The generated management handler calls the Go service in-process.
func HTTPServer(ctx context.Context, service *Service, config HTTPConfig) (http.Handler, error) {
	if err := validateHTTPConfig(service, config); err != nil {
		return nil, err
	}
	owner, err := newOwnerAuth(config.Owner)
	if err != nil {
		return nil, err
	}
	generated, err := HTTPHandler(ctx, service)
	if err != nil {
		return nil, err
	}
	gateway := &proxy{providers: config.Providers, bearers: config.Bearers}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true}`))
	})
	mux.HandleFunc("GET /auth/login", owner.login)
	mux.HandleFunc("GET /auth/callback", owner.callback)
	mux.HandleFunc("POST /auth/logout", owner.logout)
	mux.HandleFunc("GET /auth/signed-out", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		_, _ = w.Write([]byte(`<main><p>You have signed out.</p><a href="/auth/login">Sign in</a></main>`))
	})
	mux.Handle("/proxy/", gateway)
	ownerAllowed := func(w http.ResponseWriter, r *http.Request) bool {
		values := r.Header.Values("Authorization")
		if len(values) > 0 {
			principal := gateway.authenticate(r)
			if principal == nil || !principal.Owner {
				http.Error(w, "owner access required", http.StatusForbidden)
				return false
			}
			return true
		}
		if !owner.loggedIn(r) {
			http.Error(w, "not authenticated", http.StatusUnauthorized)
			return false
		}
		return true
	}
	management := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !ownerAllowed(w, r) {
			return
		}
		if !sameOrigin(r, owner.cfg.PublicBaseURL) {
			http.Error(w, "invalid origin", http.StatusForbidden)
			return
		}
		r.Body = http.MaxBytesReader(w, r.Body, maxSessionBytes*2)
		generated.ServeHTTP(w, r)
	})
	mux.Handle("/api/v1/instances", management)
	mux.Handle("/api/v1/instances/", management)
	mux.HandleFunc("GET /api/integrations/_management/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		if !ownerAllowed(w, r) {
			return
		}
		if !json.Valid(config.ManagementDocument) {
			http.NotFound(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(config.ManagementDocument)
	})
	mux.HandleFunc("GET /api/integrations/{instance}/openapi.json", func(w http.ResponseWriter, r *http.Request) {
		instance := r.PathValue("instance")
		_, ok := service.providers[instance]
		if !ok {
			http.NotFound(w, r)
			return
		}
		if len(r.Header.Values("Authorization")) > 0 {
			principal := gateway.authenticate(r)
			if principal == nil {
				http.Error(w, "not authenticated", http.StatusUnauthorized)
				return
			}
			allowed := principal.Owner
			for _, route := range principal.Routes {
				allowed = allowed || route.Instance == instance
			}
			if !allowed {
				http.Error(w, "instance access required", http.StatusForbidden)
				return
			}
		} else if !owner.loggedIn(r) {
			http.Error(w, "not authenticated", http.StatusUnauthorized)
			return
		}
		document, err := providerDocument(r.Context(), service.providers[instance], config.Providers[instance])
		if err != nil {
			http.Error(w, "provider document unavailable", http.StatusBadGateway)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(document)
	})
	for _, entry := range []struct{ path, asset, contentType string }{{"/", "status.html", "text/html; charset=utf-8"}, {"/main.css", "main.css", "text/css"}, {"/main.js", "main.js", "application/javascript"}} {
		entry := entry
		pattern := "GET " + entry.path
		if entry.path == "/" {
			pattern = "GET /{$}"
		}
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			if r.URL.Path != entry.path {
				http.NotFound(w, r)
				return
			}
			if !owner.loggedIn(r) {
				http.Redirect(w, r, "/auth/login", http.StatusSeeOther)
				return
			}
			data, ok := config.Assets[entry.asset]
			if !ok {
				http.Error(w, "asset unavailable", http.StatusInternalServerError)
				return
			}
			if entry.asset == "status.html" {
				w.Header().Set("Referrer-Policy", "no-referrer")
				w.Header().Set("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
			}
			w.Header().Set("Content-Type", entry.contentType)
			_, _ = w.Write(data)
		})
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		mux.ServeHTTP(w, r)
	}), nil
}

func sameOrigin(r *http.Request, origin string) bool {
	if r.Method == http.MethodGet || r.Method == http.MethodHead || r.Method == http.MethodOptions {
		return true
	}
	site := r.Header.Get("Sec-Fetch-Site")
	if site != "" && site != "same-origin" && site != "none" {
		return false
	}
	source := r.Header.Get("Origin")
	return source == origin || (source == "" && len(r.Header.Values("Authorization")) > 0)
}
