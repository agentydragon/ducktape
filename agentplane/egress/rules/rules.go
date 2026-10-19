// Package rules serves the sandbox-facing HTTP API on which a workload reads its own effective
// egress rules, redacted, and the agent documentation that explains how to use them.
//
// The caller's identity is the ServiceAccount its bearer proves and nothing else: no header, query
// or body selects a subject.
package rules

import (
	"context"
	"encoding/json"
	"errors"
	"io/fs"
	"log/slog"
	"net/http"
	"path"

	"github.com/agentydragon/ducktape/agentplane/egress/decision"
	"github.com/agentydragon/ducktape/agentplane/egress/identity"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// errorBody is FastAPI's error shape, which the agents of the Python proxy already read.
type errorBody struct {
	Detail string `json:"detail"`
}

type server struct {
	verifier *identity.Verifier
	index    *policy.Index
	openAPI  []byte
	docs     fs.FS
}

// New serves GET /v1/rules, /openapi.json, /llms.txt and /docs/agents/. agentDocs holds llms.txt and
// the guides it links, at its root.
func New(verifier *identity.Verifier, index *policy.Index, agentDocs fs.FS) (http.Handler, error) {
	description, err := openAPI()
	if err != nil {
		return nil, err
	}
	s := &server{verifier: verifier, index: index, openAPI: description, docs: agentDocs}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /v1/rules", s.readRules)
	mux.HandleFunc("GET /openapi.json", s.readOpenAPI)
	mux.HandleFunc("GET /llms.txt", func(w http.ResponseWriter, r *http.Request) { s.readDoc(w, r, "llms.txt") })
	mux.HandleFunc("GET /docs/agents/{name...}", func(w http.ResponseWriter, r *http.Request) {
		s.readDoc(w, r, r.PathValue("name"))
	})
	return mux, nil
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		slog.Warn("rules response not written", "error", err)
	}
}

var (
	errInvalidBearer       = errors.New("invalid workload bearer")
	errIdentityUnavailable = errors.New("workload identity unavailable")
)

// subjectOf is the ServiceAccount that the Authorization values of an HTTP request or gRPC call prove:
// errInvalidBearer when they prove none, errIdentityUnavailable when the bearer could not be reviewed.
func subjectOf(ctx context.Context, verifier *identity.Verifier, authorization []string) (policyapi.ServiceAccountRef, error) {
	token, ok := identity.BearerToken(authorization)
	if !ok {
		return policyapi.ServiceAccountRef{}, errInvalidBearer
	}
	principal, err := verifier.Identify(ctx, token)
	if err != nil {
		var rejection *identity.Rejection
		if errors.As(err, &rejection) && rejection.Reason == decision.Unavailable {
			slog.Warn("workload token could not be reviewed", "error", err)
			return policyapi.ServiceAccountRef{}, errIdentityUnavailable
		}
		return policyapi.ServiceAccountRef{}, errInvalidBearer
	}
	return policyapi.ServiceAccountRef{Namespace: principal.Account.Namespace, Name: principal.Account.Name}, nil
}

func (s *server) readRules(w http.ResponseWriter, r *http.Request) {
	subject, err := subjectOf(r.Context(), s.verifier, r.Header.Values("Authorization"))
	switch {
	case errors.Is(err, errIdentityUnavailable):
		writeJSON(w, http.StatusServiceUnavailable, errorBody{err.Error()})
	case err != nil:
		reject(w)
	default:
		writeJSON(w, http.StatusOK, ViewOf(subject, s.index.ListEffectivePolicies(subject)))
	}
}

// reject answers every bearer that does not prove a workload alike, so a response never says which
// part was wrong with it.
func reject(w http.ResponseWriter) {
	w.Header().Set("WWW-Authenticate", "Bearer")
	writeJSON(w, http.StatusUnauthorized, errorBody{errInvalidBearer.Error()})
}

func (s *server) readOpenAPI(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if _, err := w.Write(s.openAPI); err != nil {
		slog.Warn("openapi response not written", "error", err)
	}
}

// readDoc serves one file of the agent documentation. A directory or an absent name is not found.
func (s *server) readDoc(w http.ResponseWriter, r *http.Request, name string) {
	body, err := fs.ReadFile(s.docs, name)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	contentType := "text/plain; charset=utf-8"
	if path.Ext(name) == ".md" {
		contentType = "text/markdown; charset=utf-8"
	}
	w.Header().Set("Content-Type", contentType)
	if _, err := w.Write(body); err != nil {
		slog.Warn("agent documentation not written", "name", name, "error", err)
	}
}
