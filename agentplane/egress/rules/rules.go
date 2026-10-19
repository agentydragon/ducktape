// Package rules serves the sandbox-facing API on which a workload reads its own effective egress
// rules, redacted, and the agent documentation that explains how to use them.
//
// The rules are the gRPC `EgressRules` service; this package serves its generated HTTP/JSON form
// beside the documentation. The caller's identity is the ServiceAccount its bearer proves and nothing
// else: no header, query or body selects a subject.
package rules

import (
	"io/fs"
	"log/slog"
	"net/http"
	"path"
)

// New serves rulesFacade under /v1/, and /llms.txt and /docs/agents/ from agentDocs, which holds
// llms.txt and the guides it links at its root.
func New(rulesFacade http.Handler, agentDocs fs.FS) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/v1/", rulesFacade)
	mux.HandleFunc("GET /llms.txt", func(w http.ResponseWriter, r *http.Request) { readDoc(w, r, agentDocs, "llms.txt") })
	mux.HandleFunc("GET /docs/agents/{name...}", func(w http.ResponseWriter, r *http.Request) {
		readDoc(w, r, agentDocs, r.PathValue("name"))
	})
	return mux
}

// readDoc serves one file of the agent documentation. A directory or an absent name is not found.
func readDoc(w http.ResponseWriter, r *http.Request, docs fs.FS, name string) {
	body, err := fs.ReadFile(docs, name)
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
