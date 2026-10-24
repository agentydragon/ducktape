package main

import (
	"context"
	"errors"
	"net/http"
	"strings"
	"time"

	"github.com/agentydragon/ducktape/agentplane/app/go/sandboxes"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	"github.com/danielgtaylor/huma/v2"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

// SandboxView is the browser contract for the Kubernetes Sandbox and its current Pod incarnation.
type SandboxView struct {
	Name                    string                `json:"name"`
	UID                     string                `json:"uid"`
	Namespace               string                `json:"namespace"`
	CreatedAt               time.Time             `json:"created_at"`
	OperatingMode           *string               `json:"operating_mode"`
	Status                  map[string]any        `json:"status"`
	ServiceAccount          SandboxServiceAccount `json:"service_account"`
	Binding                 *SandboxBinding       `json:"binding"`
	Preset                  *string               `json:"preset"`
	WakeOnSend              bool                  `json:"wake_on_send"`
	IdleSuspendAfterSeconds *int64                `json:"idle_suspend_after_seconds"`
	SuspendedBy             *string               `json:"suspended_by"`
	Phase                   string                `json:"phase"`
	Pod                     *SandboxPodView       `json:"pod"`
}

type SandboxServiceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

type SandboxBinding struct {
	_         struct{} `nullable:"true"`
	Bootstrap string   `json:"bootstrap"`
}

type SandboxPodView struct {
	_               struct{}                `nullable:"true"`
	Name            string                  `json:"name"`
	Namespace       string                  `json:"namespace"`
	UID             string                  `json:"uid"`
	Deleting        bool                    `json:"deleting"`
	OwnerReferences []SandboxOwnerReference `json:"owner_references" nullable:"false"`
	NodeName        *string                 `json:"node_name"`
	Status          map[string]any          `json:"status"`
}

type SandboxOwnerReference struct {
	APIVersion string `json:"api_version"`
	Kind       string `json:"kind"`
	Name       string `json:"name"`
	UID        string `json:"uid"`
	Controller bool   `json:"controller"`
}

type (
	sandboxListOutput struct {
		Body []SandboxView `nullable:"false"`
	}
	sandboxOutput          struct{ Body SandboxView }
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
		UID        string `json:"uid" format:"uuid"`
		WakeOnSend bool   `json:"wake_on_send"`
	}
}

type sandboxIdleSuspendInput struct {
	Name string `path:"name" minLength:"1" maxLength:"57" pattern:"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"`
	Body struct {
		UID                     string `json:"uid" format:"uuid"`
		IdleSuspendAfterSeconds *int64 `json:"idle_suspend_after_seconds,omitempty" minimum:"60" maximum:"9223372036"`
	}
}

// RegisterSandboxRoutes installs the inventory and lifecycle routes over the namespaced Kubernetes API.
func RegisterSandboxRoutes(server *HTTPServer, config Config, inventory *sandboxes.Access) {
	api := server.ProtectedAPI()
	huma.Register[struct{}, sandboxListOutput](api, huma.Operation{
		OperationID: "listSandboxes", Method: http.MethodGet, Path: "/sandboxes", Tags: []string{"sandboxes"},
		Summary: "List Sandboxes in the configured namespace",
	}, func(ctx context.Context, _ *struct{}) (*sandboxListOutput, error) {
		values, err := inventory.ListSandboxes(ctx)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		views := make([]SandboxView, 0, len(values))
		for _, value := range values {
			views = append(views, sandboxView(value))
		}
		return &sandboxListOutput{Body: views}, nil
	})
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
		return &sandboxOutput{Body: sandboxView(value)}, nil
	})
	huma.Register[sandboxNameInput, sandboxOutput](api, huma.Operation{
		OperationID: "getSandbox", Method: http.MethodGet, Path: "/sandboxes/{name}", Tags: []string{"sandboxes"},
		Summary: "Get one Sandbox and its current Pod incarnation",
	}, func(ctx context.Context, input *sandboxNameInput) (*sandboxOutput, error) {
		value, err := inventory.Get(ctx, input.Name)
		if err != nil {
			return nil, sandboxHTTPError(err)
		}
		return &sandboxOutput{Body: sandboxView(value)}, nil
	})
	registerSandboxMutation(api, "suspendSandbox", "suspend", http.MethodPost, func(ctx context.Context, name string) error {
		return inventory.Suspend(ctx, name)
	})
	registerSandboxMutation(api, "resumeSandbox", "resume", http.MethodPost, func(ctx context.Context, name string) error {
		return inventory.Resume(ctx, name)
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
	registerSandboxMutation(api, "deleteSandbox", "", http.MethodDelete, func(ctx context.Context, name string) error {
		return inventory.Delete(ctx, name)
	})
}

func registerSandboxMutation(api huma.API, operationID, suffix string, method string, change func(context.Context, string) error) {
	path := "/sandboxes/{name}"
	if suffix != "" {
		path += "/" + suffix
	}
	huma.Register[sandboxNameInput, struct{}](api, huma.Operation{
		OperationID: operationID, Method: method, Path: path, Tags: []string{"sandboxes"}, DefaultStatus: http.StatusNoContent,
	}, func(ctx context.Context, input *sandboxNameInput) (*struct{}, error) {
		if err := change(ctx, input.Name); err != nil {
			return nil, sandboxHTTPError(err)
		}
		return nil, nil
	})
}

func sandboxView(value *sandboxespb.Sandbox) SandboxView {
	view := SandboxView{
		Name: value.GetName(), UID: value.GetUid(), Namespace: value.GetNamespace(),
		ServiceAccount: SandboxServiceAccount{}, WakeOnSend: value.GetWakeOnSend(),
		Phase: sandboxPhaseView(value.GetPhase()), Status: map[string]any{},
	}
	if value.CreatedAt != nil && value.CreatedAt.IsValid() {
		view.CreatedAt = value.CreatedAt.AsTime()
	}
	if account := value.GetServiceAccount(); account != nil {
		view.ServiceAccount = SandboxServiceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}
	}
	if binding := value.GetBinding(); binding != nil {
		view.Binding = &SandboxBinding{Bootstrap: binding.GetBootstrap()}
	}
	if value.Preset != nil {
		preset := value.GetPreset()
		view.Preset = &preset
	}
	if value.OperatingMode != nil {
		mode := value.GetOperatingMode()
		view.OperatingMode = &mode
	}
	if value.SuspendedBy != nil {
		suspendedBy := value.GetSuspendedBy()
		view.SuspendedBy = &suspendedBy
	}
	if duration := value.GetIdleSuspendAfter(); duration != nil {
		seconds := int64(duration.AsDuration().Seconds())
		view.IdleSuspendAfterSeconds = &seconds
	}
	if status := value.GetStatus(); status != nil {
		view.Status = status.AsMap()
		if _, ok := view.Status["conditions"]; !ok {
			view.Status["conditions"] = []any{}
		}
	}
	if pod := value.GetPod(); pod != nil {
		podView := &SandboxPodView{
			Name: pod.GetName(), Namespace: pod.GetNamespace(), UID: pod.GetUid(), Deleting: pod.GetDeleting(),
			NodeName: pod.NodeName, Status: map[string]any{}, OwnerReferences: make([]SandboxOwnerReference, 0, len(pod.GetOwnerReferences())),
		}
		if pod.GetStatus() != nil {
			podView.Status = pod.GetStatus().AsMap()
		}
		for _, owner := range pod.GetOwnerReferences() {
			podView.OwnerReferences = append(podView.OwnerReferences, SandboxOwnerReference{
				APIVersion: owner.GetApiVersion(), Kind: owner.GetKind(), Name: owner.GetName(), UID: owner.GetUid(), Controller: owner.GetController(),
			})
		}
		view.Pod = podView
	}
	return view
}

func sandboxPhaseView(phase sandboxespb.SandboxPhase) string {
	return map[sandboxespb.SandboxPhase]string{
		sandboxespb.SandboxPhase_SANDBOX_PHASE_UNSPECIFIED:  "Unspecified",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_CREATING:     "Creating",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_RUNNING:      "Running",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDING:   "Suspending",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_SUSPENDED:    "Suspended",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_QUIESCING:    "Quiescing",
		sandboxespb.SandboxPhase_SANDBOX_PHASE_TEARING_DOWN: "Tearing down",
	}[phase]
}

func sandboxHTTPError(err error) error {
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
