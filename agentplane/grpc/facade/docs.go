package facade

import (
	"io/fs"
	"log/slog"
	"net/http"
	"path"
)

// MountAgentDocs serves an agent API's documentation on mux: its OpenAPI document at /openapi.json, and
// /llms.txt and /docs/agents/ from agentDocs, which holds llms.txt and the guides it links at its root.
func MountAgentDocs(mux *http.ServeMux, agentDocs fs.FS, openAPI []byte) {
	mux.HandleFunc("GET /openapi.json", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if _, err := w.Write(openAPI); err != nil {
			slog.Warn("OpenAPI document not written", "error", err)
		}
	})
	mux.HandleFunc("GET /llms.txt", func(w http.ResponseWriter, r *http.Request) { readDoc(w, r, agentDocs, "llms.txt") })
	mux.HandleFunc("GET /docs/agents/{name...}", func(w http.ResponseWriter, r *http.Request) {
		readDoc(w, r, agentDocs, r.PathValue("name"))
	})
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
