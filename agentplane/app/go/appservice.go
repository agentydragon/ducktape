package main

import (
	apppb "github.com/agentydragon/ducktape/agentplane/app/appservicepb"
	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
	appbridge "github.com/agentydragon/ducktape/agentplane/app/go/bridge"
)

// appService implements the app-owned browser API in process. The HTTP gateway
// authenticates callers; it does not expose an in-cluster gRPC service.
type appService struct {
	apppb.UnimplementedAppServiceServer
	config           Config
	inventory        appbridge.SandboxGetter
	bridge           sessionOpener
	runnerStatus     runnerStatusReader
	threadWriter     threadOpener
	threadReader     threadReadStore
	threadHistory    threadReadHistory
	updates          auth.ChangeSource
	updatesConnected func() bool
	drainDone        <-chan struct{}
}
