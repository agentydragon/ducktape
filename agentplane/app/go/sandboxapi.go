package main

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/app/go/httperror"
	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

type (
	sandboxOutput struct {
		Body struct {
			Namespace string `json:"namespace" required:"true"`
			Name      string `json:"name" required:"true"`
			UID       string `json:"uid" required:"true"`
		}
	}
	sandboxTemplatesOutput struct {
		Body []string `nullable:"false"`
	}
)

type sandboxNameInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
}

type newSandboxInput struct {
	Body struct {
		Name                    string   `json:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
		Template                string   `json:"template" minLength:"1"`
		Preset                  *string  `json:"preset,omitempty"`
		PolicySets              []string `json:"policy_sets,omitempty"`
		InboxTags               []string `json:"inbox_tags,omitempty"`
		Bootstrap               string   `json:"bootstrap,omitempty" maxLength:"65536"`
		WakeOnSend              bool     `json:"wake_on_send,omitempty"`
		IdleSuspendAfterSeconds *int64   `json:"idle_suspend_after_seconds,omitempty" minimum:"60" maximum:"9223372036"`
	}
}

type sandboxWakePolicyInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		UID        string `json:"uid" minLength:"1" required:"true"`
		WakeOnSend bool   `json:"wake_on_send"`
	}
}

type sandboxIdleSuspendInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		UID                     string `json:"uid" minLength:"1" required:"true"`
		IdleSuspendAfterSeconds *int64 `json:"idle_suspend_after_seconds,omitempty" minimum:"60" maximum:"9223372036"`
	}
}

type sandboxDeleteInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		UID string `json:"uid" minLength:"1" required:"true"`
	}
}

type sandboxLifecycleInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		UID string `json:"uid" minLength:"1" required:"true"`
	}
}

type environmentResumer interface {
	ResumeEnvironment(context.Context, *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error)
}

// RegisterSandboxRoutes installs Sandbox creation and lifecycle routes. Readable Sandbox state is
// served by the caller-authorized EnvironmentService facade.
func RegisterSandboxRoutes(server *HTTPServer, config Config, inventory *sandboxes.Access, environmentClient environmentResumer) {
	api := server.ProtectedAPI()
	huma.Register[struct{}, sandboxTemplatesOutput](api, huma.Operation{
		OperationID: "listSandboxTemplates", Method: http.MethodGet, Path: "/sandboxes/templates", Tags: []string{"sandboxes"},
		Summary: "List templates available for a new Sandbox",
	}, func(ctx context.Context, _ *struct{}) (*sandboxTemplatesOutput, error) {
		values, err := inventory.ListTemplates(ctx)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		return &sandboxTemplatesOutput{Body: values}, nil
	})
	huma.Register[newSandboxInput, sandboxOutput](api, huma.Operation{
		OperationID: "createSandbox", Method: http.MethodPost, Path: "/sandboxes", Tags: []string{"sandboxes"},
		Summary: "Create a Sandbox from explicit launch choices", DefaultStatus: http.StatusCreated,
	}, func(ctx context.Context, input *newSandboxInput) (*sandboxOutput, error) {
		body := input.Body
		if body.Preset != nil {
			if _, ok := config.SandboxPresets[*body.Preset]; !ok {
				return nil, huma.Error422UnprocessableEntity("no preset named " + quote(*body.Preset))
			}
		}
		request := &sandboxespb.CreateSandboxRequest{
			Name: body.Name, Template: body.Template, Preset: body.Preset,
			PolicySets: nonNilStrings(body.PolicySets), InboxTags: nonNilStrings(body.InboxTags),
			Bootstrap: body.Bootstrap, WakeOnSend: body.WakeOnSend,
		}
		if body.IdleSuspendAfterSeconds != nil {
			request.IdleSuspendAfter = &durationpb.Duration{Seconds: *body.IdleSuspendAfterSeconds}
			if err := request.IdleSuspendAfter.CheckValid(); err != nil {
				return nil, huma.Error422UnprocessableEntity("idle suspend duration is invalid")
			}
		}
		value, err := inventory.Create(ctx, request)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		return sandboxCreatedOutput(value), nil
	})
	registerSandboxMutation(api, "suspendSandbox", "suspend", http.MethodPost, func(ctx context.Context, name, uid string) error {
		return inventory.Suspend(ctx, name, uid)
	})
	registerSandboxMutation(api, "resumeSandbox", "resume", http.MethodPost, func(ctx context.Context, name, uid string) error {
		return requestSandboxResume(ctx, config.SandboxNamespace, name, uid, environmentClient)
	})
	huma.Register[sandboxWakePolicyInput, struct{}](api, huma.Operation{
		OperationID: "setSandboxWakePolicy", Method: http.MethodPatch, Path: "/sandboxes/{name}/wake-policy", Tags: []string{"sandboxes"},
		Summary: "Set whether messages may wake this Sandbox incarnation",
	}, func(ctx context.Context, input *sandboxWakePolicyInput) (*struct{}, error) {
		if err := inventory.SetWakeOnSend(ctx, input.Name, input.Body.UID, input.Body.WakeOnSend); err != nil {
			return nil, sandboxHTTPError(err)
		}
		return nil, nil
	})
	huma.Register[sandboxIdleSuspendInput, struct{}](api, huma.Operation{
		OperationID: "setSandboxIdleSuspend", Method: http.MethodPatch, Path: "/sandboxes/{name}/idle-suspend", Tags: []string{"sandboxes"},
		Summary: "Set the idle suspension delay for this Sandbox incarnation",
	}, func(ctx context.Context, input *sandboxIdleSuspendInput) (*struct{}, error) {
		var duration *durationpb.Duration
		if seconds := input.Body.IdleSuspendAfterSeconds; seconds != nil {
			duration = &durationpb.Duration{Seconds: *seconds}
			if err := duration.CheckValid(); err != nil {
				return nil, huma.Error422UnprocessableEntity("idle suspend duration is invalid")
			}
		}
		if err := inventory.SetIdleSuspendAfter(ctx, input.Name, input.Body.UID, duration); err != nil {
			return nil, sandboxHTTPError(err)
		}
		return nil, nil
	})
	huma.Register[sandboxDeleteInput, struct{}](api, huma.Operation{
		OperationID: "deleteSandbox", Method: http.MethodDelete, Path: "/sandboxes/{name}", Tags: []string{"sandboxes"},
		Summary: "Begin teardown of the exact Sandbox incarnation", DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *sandboxDeleteInput) (*struct{}, error) {
		if err := inventory.Delete(ctx, input.Name, input.Body.UID); err != nil {
			return nil, sandboxHTTPError(err)
		}
		return nil, nil
	})
}

func requestSandboxResume(ctx context.Context, namespace, name, uid string, environmentClient environmentResumer) error {
	if environmentClient == nil || namespace == "" || name == "" || uid == "" {
		return errors.New("EnvironmentService or Sandbox identity is unavailable")
	}
	target := &environmentpb.TargetHandle{Namespace: namespace, Name: name, Uid: uid}
	response, err := environmentClient.ResumeEnvironment(ctx, &environmentpb.ResumeEnvironmentRequest{Target: target})
	if err != nil {
		return err
	}
	if response == nil || response.GetTarget() == nil || response.GetTarget().GetNamespace() != namespace ||
		response.GetTarget().GetName() != name || response.GetTarget().GetUid() != uid {
		return errors.New("EnvironmentService returned a different Sandbox identity")
	}
	if response.GetOutcome() == environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED {
		return grpcstatus.Error(codes.Aborted, "environment state changed; refresh and try again")
	}
	if response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED &&
		response.GetOutcome() != environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING {
		return errors.New("EnvironmentService returned no resume outcome")
	}
	return nil
}

func sandboxCreatedOutput(value *environmentpb.TargetHandle) *sandboxOutput {
	output := &sandboxOutput{}
	if value != nil {
		output.Body.Namespace = value.GetNamespace()
		output.Body.Name = value.GetName()
		output.Body.UID = value.GetUid()
	}
	return output
}

func registerSandboxMutation(api huma.API, operationID, suffix string, method string, change func(context.Context, string, string) error) {
	path := "/sandboxes/{name}"
	if suffix != "" {
		path += "/" + suffix
	}
	huma.Register[sandboxLifecycleInput, struct{}](api, huma.Operation{
		OperationID: operationID, Method: method, Path: path, Tags: []string{"sandboxes"}, DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *sandboxLifecycleInput) (*struct{}, error) {
		if err := change(ctx, input.Name, input.Body.UID); err != nil {
			return nil, sandboxHTTPError(err)
		}
		return nil, nil
	})
}

func sandboxHTTPError(err error) error {
	return httperror.LogServerError(classifySandboxError(err), err)
}

func classifySandboxError(err error) error {
	var notFound *sandboxes.SandboxNotFoundError
	var invalid *sandboxes.InvalidSandboxError
	var conflict *sandboxes.SandboxConflictError
	var running *sandboxes.SandboxRunningError
	var modeUnknown *sandboxes.SandboxModeUnknownError
	var tearingDown *sandboxes.SandboxTearingDownError
	var quiescing *sandboxes.SandboxQuiescingError
	switch {
	case errors.As(err, &notFound), apierrors.IsNotFound(err):
		return huma.Error404NotFound(err.Error())
	case errors.As(err, &invalid):
		return huma.Error422UnprocessableEntity(err.Error())
	case errors.As(err, &conflict), errors.As(err, &running), errors.As(err, &modeUnknown), errors.As(err, &tearingDown):
		return huma.Error409Conflict(err.Error())
	case errors.As(err, &quiescing):
		return huma.Error503ServiceUnavailable(err.Error())
	default:
		switch grpcstatus.Code(err) {
		case codes.NotFound:
			return huma.Error404NotFound(err.Error())
		case codes.InvalidArgument:
			return huma.Error422UnprocessableEntity(err.Error())
		case codes.FailedPrecondition, codes.AlreadyExists, codes.Aborted:
			return huma.Error409Conflict(err.Error())
		case codes.ResourceExhausted:
			return huma.Error429TooManyRequests(err.Error())
		case codes.DeadlineExceeded:
			return huma.Error504GatewayTimeout("Upstream deadline expired")
		}
		if apierrors.IsConflict(err) {
			return huma.Error409Conflict(err.Error())
		}
		if errors.Is(err, context.DeadlineExceeded) || strings.Contains(strings.ToLower(err.Error()), "timeout") {
			return huma.Error504GatewayTimeout("Upstream deadline expired")
		}
		return huma.Error503ServiceUnavailable("Upstream service unavailable")
	}
}

func quote(value string) string { return "'" + strings.ReplaceAll(value, "'", "\\'") + "'" }
