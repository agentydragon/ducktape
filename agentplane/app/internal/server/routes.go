package server

import (
	"fmt"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/linkage_servicepb"
	apphttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi"
	operatorhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/operator"
	sessionhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/sessions"
	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
	threadhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/threads"
)

// routeOwners are the resource handlers composed by Run at the authenticated HTTP boundary.
type routeOwners struct {
	appConfig       *apphttp.AppConfigHandler
	sessions        *sessionhttp.Handler
	threads         *threadhttp.Handler
	operatorLinkage linkage_servicepb.LinkageServiceClient
	systemStatus    *apphttp.SystemStatusHandler
	streams         streamhttp.Dependencies
	traceRelay      http.Handler
}

func registerRoutes(server *httpServer, owners routeOwners) error {
	if err := owners.appConfig.RegisterRoutes(server); err != nil {
		return fmt.Errorf("register app configuration facade: %w", err)
	}
	if err := owners.sessions.RegisterRoutes(server); err != nil {
		return fmt.Errorf("register Session and Environment Runtime routes: %w", err)
	}
	if err := owners.threads.RegisterRoutes(server); err != nil {
		return fmt.Errorf("register Thread metadata facade: %w", err)
	}
	operatorhttp.RegisterRoutes(server.mux, owners.operatorLinkage)
	if err := owners.systemStatus.RegisterRoutes(server); err != nil {
		return fmt.Errorf("register System status facade: %w", err)
	}
	server.RegisterRawRoute("POST /telemetry/v1/traces", owners.traceRelay, true)
	streamhttp.RegisterRoutes(server, server.DrainDone(), owners.streams)
	return nil
}
