package server

import (
	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	sessionhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/sessions"
	threadhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/threads"
)

// appService implements the app-owned browser API in process. The HTTP gateway
// authenticates callers; it does not expose an in-cluster gRPC service.
type appService struct {
	apppb.UnimplementedAppServiceServer
	sessions       *sessionhttp.Handler
	threadMetadata *threadhttp.Handler
}
