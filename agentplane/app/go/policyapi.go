package main

import (
	"context"
	"net/http"

	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/danielgtaylor/huma/v2"
)

type sandboxFormOutput struct {
	Body struct {
		PolicySets         []string           `json:"policy_sets" nullable:"false"`
		NewSandboxDefaults NewSandboxDefaults `json:"new_sandbox_defaults"`
	}
}

// RegisterPolicyRoutes installs the Sandbox form's policy-set inventory.
func RegisterPolicyRoutes(server *HTTPServer, config Config, policies *policyaccess.Access) {
	readAPI := server.ProtectedAPI()
	huma.Register[struct{}, sandboxFormOutput](readAPI, huma.Operation{
		OperationID: "getSandboxForm", Method: http.MethodGet, Path: "/sandboxes/form", Tags: []string{"sandboxes"},
		Summary: "List policy sets and lifecycle defaults for a new Sandbox",
	}, func(ctx context.Context, _ *struct{}) (*sandboxFormOutput, error) {
		names, err := policies.PolicySetNames(ctx)
		if err != nil {
			return nil, huma.Error503ServiceUnavailable("PolicySet inventory is unavailable")
		}
		output := &sandboxFormOutput{}
		output.Body.PolicySets = names
		output.Body.NewSandboxDefaults = config.NewSandboxDefaults
		return output, nil
	})
}
