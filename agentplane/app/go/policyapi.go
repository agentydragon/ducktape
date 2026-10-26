package main

import (
	"context"
	"errors"
	"net/http"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/policyaccess"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	"github.com/danielgtaylor/huma/v2"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type sandboxFormOutput struct {
	Body struct {
		PolicySets         []string           `json:"policy_sets" nullable:"false"`
		NewSandboxDefaults NewSandboxDefaults `json:"new_sandbox_defaults"`
	}
}

type sandboxEgressOutput struct {
	Body []policyaccess.BindingView `nullable:"false"`
}

type policyBindingOutput struct {
	Body policyaccess.BindingView
}

type sandboxPolicyGrantInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		PolicySets []string `json:"policy_sets" minItems:"1"`
		ExpiresAt  *string  `json:"expires_at,omitempty" format:"date-time"`
	}
}

type policyBindingNameInput struct {
	Name string `path:"name" minLength:"1"`
}

// RegisterPolicyRoutes installs the caller-readable Egress views and operator-only grant/revoke
// operations. Access uses the existing typed PolicyBinding, PolicySet, and EgressCredential CRDs.
func RegisterPolicyRoutes(server *HTTPServer, config Config, inventory *sandboxes.Access, policies *policyaccess.Access) {
	readAPI := server.ProtectedAPI()
	huma.Register[struct{}, sandboxFormOutput](readAPI, huma.Operation{
		OperationID: "getSandboxForm", Method: http.MethodGet, Path: "/sandboxes/form", Tags: []string{"sandboxes"},
		Summary: "List policy sets and lifecycle defaults for a new Sandbox",
	}, func(ctx context.Context, _ *struct{}) (*sandboxFormOutput, error) {
		names, err := policies.PolicySetNames(ctx)
		if err != nil {
			return nil, policyHTTPError(err)
		}
		output := &sandboxFormOutput{}
		output.Body.PolicySets = names
		output.Body.NewSandboxDefaults = config.NewSandboxDefaults
		return output, nil
	})
	huma.Register[sandboxNameInput, sandboxEgressOutput](readAPI, huma.Operation{
		OperationID: "listSandboxEgress", Method: http.MethodGet, Path: "/sandboxes/{name}/egress", Tags: []string{"sandboxes"},
		Summary: "List policy bindings for a Sandbox's ServiceAccount",
	}, func(ctx context.Context, input *sandboxNameInput) (*sandboxEgressOutput, error) {
		sandbox, err := inventory.Get(ctx, input.Name)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		bindings, err := policies.BindingsFor(ctx, sandbox.GetServiceAccount())
		if err != nil {
			return nil, policyHTTPError(err)
		}
		return &sandboxEgressOutput{Body: bindings}, nil
	})
	operatorAPI := server.OperatorAPI()
	huma.Register[sandboxPolicyGrantInput, policyBindingOutput](operatorAPI, huma.Operation{
		OperationID: "grantSandboxPolicySets", Method: http.MethodPost, Path: "/sandboxes/{name}/policy-bindings", Tags: []string{"sandboxes"},
		Summary: "Grant policy sets to a running Sandbox", DefaultStatus: http.StatusCreated,
	}, func(ctx context.Context, input *sandboxPolicyGrantInput) (*policyBindingOutput, error) {
		sandbox, err := inventory.Get(ctx, input.Name)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		var expiresAt *metav1.Time
		if input.Body.ExpiresAt != nil {
			parsed, err := time.Parse(time.RFC3339Nano, *input.Body.ExpiresAt)
			if err != nil {
				return nil, huma.Error422UnprocessableEntity("expires_at must be an RFC 3339 datetime with a timezone")
			}
			value := metav1.NewTime(parsed.UTC())
			expiresAt = &value
		}
		binding, err := policies.Grant(ctx, sandbox, input.Body.PolicySets, expiresAt)
		if err != nil {
			return nil, policyHTTPError(err)
		}
		return &policyBindingOutput{Body: *binding}, nil
	})
	huma.Register[policyBindingNameInput, struct{}](operatorAPI, huma.Operation{
		OperationID: "revokePolicyBinding", Method: http.MethodDelete, Path: "/policy-bindings/{name}", Tags: []string{"policy-bindings"},
		Summary: "Delete an operator-owned PolicyBinding", DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *policyBindingNameInput) (*struct{}, error) {
		if err := policies.Revoke(ctx, input.Name); err != nil {
			return nil, policyHTTPError(err)
		}
		return nil, nil
	})
}

func policyHTTPError(err error) error {
	var notFound *policyaccess.BindingNotFoundError
	var missingSets *sandboxes.UnknownPolicySetError
	var fromGit *policyaccess.FluxOwnedBindingError
	var fromSandbox *policyaccess.SandboxOwnedBindingError
	var notVisible *policyaccess.CreatedBindingNotVisibleError
	switch {
	case errors.As(err, &notFound):
		return huma.Error404NotFound(err.Error())
	case errors.As(err, &missingSets):
		return huma.Error422UnprocessableEntity(err.Error())
	case errors.As(err, &fromGit), errors.As(err, &fromSandbox):
		return huma.Error409Conflict(err.Error())
	case errors.As(err, &notVisible):
		return huma.Error503ServiceUnavailable(err.Error())
	default:
		return huma.Error503ServiceUnavailable("Policy access is unavailable")
	}
}
