// Package discovery exposes only existing managed Sandboxes whose live execution
// ServiceAccount is the reviewed caller's exact account UID.
package discovery

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const maxLimit = 100

// Service serves the inventory of one namespace. The client is uncached so every page and exact
// lookup starts from a current API-server view; ExecutionAccount rereads each candidate's UID and
// controller-owned account before it is returned.
type Service struct {
	discoverypb.UnimplementedEnvironmentDiscoveryServer

	namespace   string
	sandboxes   *sandboxreader.Reader
	runtime     runtimeconfig.Catalog
	wakeCallers map[auth.Account]struct{}
}

func New(
	namespace string,
	objects client.Reader,
	runtime runtimeconfig.Catalog,
	wakeCallers map[auth.Account]struct{},
) (*Service, error) {
	if namespace == "" || objects == nil {
		return nil, errors.New("Sandbox namespace and uncached Kubernetes reader are required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	wake := make(map[auth.Account]struct{}, len(wakeCallers))
	for account := range wakeCallers {
		wake[account] = struct{}{}
	}
	return &Service{namespace: namespace, sandboxes: sandboxes, runtime: runtime, wakeCallers: wake}, nil
}

// Methods admits only TokenReview-verified ServiceAccounts in the configured Resolver namespaces.
// Object-level authority is decided by the handler using the live account UID.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		discoverypb.EnvironmentDiscovery_ListEnvironments_FullMethodName: rule,
		discoverypb.EnvironmentDiscovery_GetEnvironment_FullMethodName:   rule,
	}
}

func caller(ctx context.Context) (auth.ServiceAccountPrincipal, error) {
	reviewed, err := auth.AdmittedReviewedCaller(ctx)
	if err != nil {
		return auth.ServiceAccountPrincipal{}, err
	}
	account := reviewed.ServiceAccount
	if account.Account.Namespace == "" || account.Account.Name == "" || account.ServiceAccountUID == "" {
		return auth.ServiceAccountPrincipal{}, status.Error(codes.Unauthenticated, "reviewed ServiceAccount identity is incomplete")
	}
	return account, nil
}

func callerMatches(caller auth.ServiceAccountPrincipal, execution sandboxreader.ServiceAccountIdentity) bool {
	return caller.Account.Namespace == execution.Namespace && caller.Account.Name == execution.Name &&
		caller.ServiceAccountUID == execution.UID
}

func (s *Service) ListEnvironments(ctx context.Context, request *discoverypb.ListEnvironmentsRequest) (*discoverypb.ListEnvironmentsResponse, error) {
	if request == nil || request.GetLimit() < 1 || request.GetLimit() > maxLimit {
		return nil, status.Errorf(codes.InvalidArgument, "limit must be between 1 and %d", maxLimit)
	}
	identity, err := caller(ctx)
	if err != nil {
		return nil, err
	}
	if request.GetPageToken() != "" {
		return nil, status.Error(codes.InvalidArgument, "page token is not valid for this inventory")
	}
	response := &discoverypb.ListEnvironmentsResponse{Items: []*discoverypb.EnvironmentDescriptor{}}
	// Current Sandbox-owned execution accounts share the Sandbox's namespace and name. Read only
	// that candidate directly; a namespace-wide continuation token could reveal another Sandbox's
	// storage key even when its object is filtered out. The list contract keeps its page shape for
	// future account mappings without making today's lookup depend on foreign inventory.
	if identity.Account.Namespace != s.namespace {
		return response, nil
	}
	view, err := s.sandboxes.Get(ctx, identity.Account.Name)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return response, nil
		}
		slog.ErrorContext(ctx, "Environment discovery Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	if deleting(view.Resource) {
		return response, nil
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, view.Identity)
	if err != nil {
		slog.ErrorContext(ctx, "Environment discovery execution account read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
	}
	if found && callerMatches(identity, execution) {
		response.Items = append(response.Items, s.describe(view.Resource, execution, identity))
	}
	return response, nil
}

func (s *Service) GetEnvironment(ctx context.Context, request *discoverypb.GetEnvironmentRequest) (*discoverypb.EnvironmentDescriptor, error) {
	identity, err := caller(ctx)
	if err != nil {
		return nil, err
	}
	target := request.GetTarget()
	if target == nil || target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "complete target handle is required")
	}
	if target.GetNamespace() != s.namespace {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	view, err := s.sandboxes.Get(ctx, target.GetName())
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, status.Error(codes.NotFound, "environment not found")
		}
		slog.ErrorContext(ctx, "Environment discovery Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	if view.Identity.UID != target.GetUid() || deleting(view.Resource) {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, view.Identity)
	if err != nil {
		slog.ErrorContext(ctx, "Environment discovery execution account read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
	}
	if !found || !callerMatches(identity, execution) {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	return s.describe(view.Resource, execution, identity), nil
}

func deleting(object *api.Sandbox) bool {
	return object.DeletionTimestamp != nil && !object.DeletionTimestamp.IsZero()
}

func (s *Service) describe(
	object *api.Sandbox,
	execution sandboxreader.ServiceAccountIdentity,
	caller auth.ServiceAccountPrincipal,
) *discoverypb.EnvironmentDescriptor {
	condition := meta.FindStatusCondition(object.Status.Conditions, api.ConditionReady)
	readiness := &discoverypb.Readiness{State: discoverypb.ReadinessState_READINESS_STATE_NOT_READY}
	if condition == nil {
		readiness.Reason = "ReadyConditionUnavailable"
	} else {
		readiness.Detail = condition.Message
		readiness.Reason = condition.Reason
		if condition.Status == metav1.ConditionTrue {
			readiness.State = discoverypb.ReadinessState_READINESS_STATE_READY
		}
	}
	mode := discoverypb.OperatingMode_OPERATING_MODE_UNSPECIFIED
	resume := discoverypb.ResumeState_RESUME_STATE_UNSPECIFIED
	switch object.Status.OperatingMode {
	case api.OperatingModeRunning:
		mode = discoverypb.OperatingMode_OPERATING_MODE_RUNNING
		resume = discoverypb.ResumeState_RESUME_STATE_NOT_REQUIRED
	case api.OperatingModeSuspended:
		mode = discoverypb.OperatingMode_OPERATING_MODE_SUSPENDED
		_, callerCanWake := s.wakeCallers[auth.Account{Namespace: caller.Account.Namespace, Name: caller.Account.Name}]
		if object.Status.StopStage != api.StopStageQuiescing && object.Status.StopStage != api.StopStageDeleting &&
			object.Spec.WakeOnSend && callerCanWake {
			resume = discoverypb.ResumeState_RESUME_STATE_AVAILABLE
		} else {
			resume = discoverypb.ResumeState_RESUME_STATE_NOT_AVAILABLE
		}
	}
	descriptor := &discoverypb.EnvironmentDescriptor{
		Target: &discoverypb.TargetHandle{Namespace: object.Namespace, Name: object.Name, Uid: string(object.UID)},
		ExecutionAccount: &discoverypb.AccountHandle{
			Namespace: execution.Namespace, Name: execution.Name, Uid: execution.UID,
		},
		Readiness:                  readiness,
		Workspace:                  &discoverypb.WorkspaceGuidance{Availability: discoverypb.Availability_AVAILABILITY_UNAVAILABLE},
		Instructions:               &discoverypb.TextFact{Availability: discoverypb.Availability_AVAILABILITY_ABSENT},
		ConfigurationsAvailability: discoverypb.Availability_AVAILABILITY_UNAVAILABLE,
		ObservedAt:                 timestamppb.New(time.Now().UTC()),
		Template:                   object.Spec.Template,
		OperatingMode:              mode,
		StopStage:                  string(object.Status.StopStage),
		WakeOnSend:                 object.Spec.WakeOnSend,
		Resume:                     resume,
	}
	if object.Status.Incarnation != nil {
		descriptor.RuntimeIncarnationUid = object.Status.Incarnation.UID
	}
	template, known := s.runtime.Templates[object.Spec.Template]
	if !known {
		return descriptor
	}
	if template.WorkspaceRoot == "" {
		descriptor.Workspace.Availability = discoverypb.Availability_AVAILABILITY_ABSENT
	} else if template.WorkspaceProvenance == runtimeconfig.ProvenanceDeploymentConfig {
		descriptor.Workspace.Availability = discoverypb.Availability_AVAILABILITY_CONFIGURED
		descriptor.Workspace.Root = template.WorkspaceRoot
		descriptor.Workspace.Provenance = discoverypb.Provenance_PROVENANCE_DEPLOYMENT_CONFIG
		descriptor.Workspace.RootEnforced = template.WorkspaceRootEnforced
		descriptor.Workspace.CreatesMissingCwd = template.CreatesMissingCWD
	}
	descriptor.Configurations = configuredChoices(s.runtime.Models)
	if len(descriptor.Configurations) == 0 {
		descriptor.ConfigurationsAvailability = discoverypb.Availability_AVAILABILITY_ABSENT
	} else {
		descriptor.ConfigurationsAvailability = discoverypb.Availability_AVAILABILITY_CONFIGURED
	}
	return descriptor
}

func configuredChoices(catalog runtimeconfig.ModelCatalog) []*discoverypb.RuntimeConfiguration {
	models := make(map[string]runtimeconfig.ModelOption, len(catalog.Models))
	for _, model := range catalog.Models {
		models[model.Model] = model
	}
	harnesses := make([]string, 0, len(catalog.Harnesses))
	for harness := range catalog.Harnesses {
		harnesses = append(harnesses, harness)
	}
	slices.Sort(harnesses)
	var result []*discoverypb.RuntimeConfiguration
	for _, harness := range harnesses {
		for _, modelID := range catalog.Harnesses[harness] {
			model, ok := models[modelID]
			if !ok {
				continue
			}
			result = append(result, &discoverypb.RuntimeConfiguration{
				Harness: harness, Model: model.Model, DisplayName: model.DisplayName,
				ReasoningEfforts: stringsOrEmpty(model.ReasoningEfforts),
				Provenance:       discoverypb.Provenance_PROVENANCE_DEPLOYMENT_CONFIG,
			})
		}
	}
	return result
}

func stringsOrEmpty(values []string) []string {
	if values == nil {
		return []string{}
	}
	return values
}

var _ discoverypb.EnvironmentDiscoveryServer = (*Service)(nil)
