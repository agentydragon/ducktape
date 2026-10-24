package main

import (
	"context"
	"encoding/json"
	"net/http"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/protobuf/encoding/protojson"
)

type runnerStatusReader interface {
	RunnerStatus(context.Context, string) (*runnerpb.RunnerStatus, error)
}

type runnerStatusOutput struct {
	Body map[string]any
}

func RegisterRunnerRoutes(server *HTTPServer, bridge runnerStatusReader) {
	huma.Register[sandboxNameInput, runnerStatusOutput](server.ProtectedAPI(), huma.Operation{
		OperationID: "getSandboxRunnerStatus", Method: http.MethodGet, Path: "/sandboxes/{name}/runner-status", Tags: []string{"sandboxes"},
		Summary: "Read the current live Runner status without waking the Sandbox",
	}, func(ctx context.Context, input *sandboxNameInput) (*runnerStatusOutput, error) {
		status, err := bridge.RunnerStatus(ctx, input.Name)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		encoded, err := (protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}).Marshal(status)
		if err != nil {
			return nil, huma.Error500InternalServerError("Runner status could not be encoded")
		}
		var view map[string]any
		if err := json.Unmarshal(encoded, &view); err != nil {
			return nil, huma.Error500InternalServerError("Runner status could not be decoded")
		}
		return &runnerStatusOutput{Body: view}, nil
	})
}
