package linkage

import (
	"errors"
	"log/slog"
	"net/http"

	"google.golang.org/protobuf/encoding/protojson"
)

// Mount serves the linkage's public routes on routes: the callback at CallbackPath and the shared Client ID Metadata
// Document at ClientMetadataPath. Neither takes a bearer: the callback is authorized by its flow's state.
func (a *Authority) Mount(routes *http.ServeMux) {
	routes.HandleFunc("GET "+CallbackPath, a.serveCallback)
	routes.HandleFunc("GET "+ClientMetadataPath, a.serveClientMetadata)
}

// serveCallback finishes a flow and answers with the server's linkage, in protojson. A missing state or code is 400,
// a flow that is unknown, spent or expired 409, one whose server is no longer configured 404, and a code the token
// endpoint did not exchange 502, with what it answered.
func (a *Authority) serveCallback(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	state, code := query.Get("state"), query.Get("code")
	if state == "" || code == "" {
		http.Error(w, "OAuth callback is missing state or code", http.StatusBadRequest)
		return
	}
	linked, err := a.Callback(r.Context(), state, code)
	var refused *tokenError
	switch {
	case errors.Is(err, ErrInvalidFlow):
		http.Error(w, ErrInvalidFlow.Error(), http.StatusConflict)
		return
	case errors.Is(err, ErrUnknownServer):
		http.Error(w, ErrUnknownServer.Error(), http.StatusNotFound)
		return
	case errors.As(err, &refused):
		http.Error(w, refused.message, http.StatusBadGateway)
		return
	case err != nil:
		slog.WarnContext(r.Context(), "MCP linkage callback failed", "error", err)
		http.Error(w, "the Action Service is temporarily unavailable", http.StatusServiceUnavailable)
		return
	}
	body, err := protojson.MarshalOptions{EmitUnpopulated: true}.Marshal(linked)
	if err != nil {
		slog.ErrorContext(r.Context(), "MCP linkage cannot be shown", "error", err)
		http.Error(w, "the linkage cannot be shown", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(body)
}

// serveClientMetadata answers the shared Client ID Metadata Document, which authorization servers fetch and may cache
// for an hour; 404 when no server uses it.
func (a *Authority) serveClientMetadata(w http.ResponseWriter, r *http.Request) {
	if a.document == nil {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.Header().Set("Cache-Control", "public, max-age=3600")
	_, _ = w.Write(a.document)
}
