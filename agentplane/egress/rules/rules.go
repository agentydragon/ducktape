// Package rules serves the sandbox-facing API on which a workload reads its own effective egress
// rules, redacted, and the agent documentation that explains how to use them.
//
// The rules are the gRPC `EgressRules` service; this package serves its generated HTTP/JSON form
// beside the documentation. The caller's identity is the ServiceAccount its bearer proves and nothing
// else: no header, query or body selects a subject.
package rules

import (
	"io/fs"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
)

// New serves rulesFacade under /v1/, and the documentation facade.MountAgentDocs serves.
func New(rulesFacade http.Handler, agentDocs fs.FS, openAPI []byte) http.Handler {
	mux := http.NewServeMux()
	mux.Handle("/v1/", rulesFacade)
	facade.MountAgentDocs(mux, agentDocs, openAPI)
	return mux
}
