package main

import (
	"context"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/app/go/status"
	"github.com/danielgtaylor/huma/v2"
)

type systemStatusOutput struct {
	Body []status.ComponentStatus
}

func RegisterStatusRoutes(server *HTTPServer, checker *status.Checker) {
	huma.Register[struct{}, systemStatusOutput](server.ProtectedAPI(), huma.Operation{
		OperationID: "readSystemStatus", Method: http.MethodGet, Path: "/system/status", Tags: []string{"system"},
		Summary: "Check the configured dependent services",
	}, func(ctx context.Context, _ *struct{}) (*systemStatusOutput, error) {
		return &systemStatusOutput{Body: checker.CheckAll(ctx)}, nil
	})
}
