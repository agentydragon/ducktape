package server

import (
	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	threadhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/threads"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/app/internal/status"
)

// appService implements the app-owned browser API in process. The HTTP gateway
// authenticates callers; it does not expose an in-cluster gRPC service.
type appService struct {
	apppb.UnimplementedAppServiceServer
	config         settings.Config
	systemStatus   *status.Checker
	policySetNames policySetNameReader
	environments   sessionEnvironmentReader
	bridge         sessionOpener
	runnerStatus   runnerStatusReader
	threadWriter   threadOpener
	threadMetadata *threadhttp.Handler
}
