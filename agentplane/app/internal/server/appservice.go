package server

import (
	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	"github.com/agentydragon/ducktape/agentplane/app/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/app/internal/status"
)

// appService implements the app-owned browser API in process. The HTTP gateway
// authenticates callers; it does not expose an in-cluster gRPC service.
type appService struct {
	apppb.UnimplementedAppServiceServer
	config           settings.Config
	systemStatus     *status.Checker
	policySetNames   policySetNameReader
	environments     sessionEnvironmentReader
	bridge           sessionOpener
	runnerStatus     runnerStatusReader
	threadWriter     threadOpener
	threadReader     threadReadStore
	threadHistory    threadReadHistory
	threadRuntime    threadEnvironmentRuntimeStatusReader
	updates          auth.ChangeSource
	updatesConnected func() bool
	drainDone        <-chan struct{}
}
