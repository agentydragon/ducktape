// Package discovery exposes only existing managed Sandboxes whose live execution
// ServiceAccount is the reviewed caller's exact account UID.
package discovery

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"time"

	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle"
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

	namespace string
	sandboxes *sandboxreader.Reader
	runtime   runtimeconfig.Catalog
	lifecycle *lifecycle.Service
}

func New(
	namespace string,
	objects client.Reader,
	runtime runtimeconfig.Catalog,
	lifecycleService *lifecycle.Service,
) (*Service, error) {
	if namespace == "" || objects == nil || lifecycleService == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes reader, and lifecycle service are required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Service{namespace: namespace, sandboxes: sandboxes, runtime: runtime, lifecycle: lifecycleService}, nil
}

// Methods admits direct TokenReview-verified ServiceAccounts in the configured Resolver namespaces.
// The composition root may enable the shared delegated mode on selected methods; object-level
// authority is decided by the handler using the effective caller's live account UID.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		discoverypb.EnvironmentDiscovery_ListEnvironments_FullMethodName: rule,
		discoverypb.EnvironmentDiscovery_GetEnvironment_FullMethodName:   rule,
	}
}

func caller(ctx context.Context) (auth.ServiceAccountPrincipal, error) {
	identity, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return auth.ServiceAccountPrincipal{}, err
	}
	if identity.Account.Namespace == "" || identity.Account.Name == "" || identity.ServiceAccountUID == "" {
		return auth.ServiceAccountPrincipal{}, status.Error(codes.Unauthenticated, "effective ServiceAccount identity is incomplete")
	}
	return identity, nil
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
		descriptor, err := s.describe(ctx, view.Resource, execution, identity)
		if err != nil {
			return nil, err
		}
		response.Items = append(response.Items, descriptor)
	}
	return response, nil
}

func (s *Service) GetEnvironment(ctx context.Context, request *discoverypb.GetEnvironmentRequest) (*discoverypb.EnvironmentDescriptor, error) {
	if request == nil || request.GetWaitForReadySeconds() > 30 {
		return nil, status.Error(codes.InvalidArgument, "wait_for_ready_seconds must be between 0 and 30")
	}
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
	descriptor, err := s.getEnvironment(ctx, target.GetName(), target.GetUid(), identity)
	if err != nil || request.GetWaitForReadySeconds() == 0 || descriptor.GetReadiness().GetState() == discoverypb.ReadinessState_READINESS_STATE_READY {
		return descriptor, err
	}
	timer := time.NewTimer(time.Duration(request.GetWaitForReadySeconds()) * time.Second)
	defer timer.Stop()
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		case <-ticker.C:
			descriptor, err = s.getEnvironment(ctx, target.GetName(), target.GetUid(), identity)
			if err != nil || descriptor.GetReadiness().GetState() == discoverypb.ReadinessState_READINESS_STATE_READY {
				return descriptor, err
			}
		case <-timer.C:
			// Return facts read at the wait boundary, not the earlier descriptor that started the wait.
			return s.getEnvironment(ctx, target.GetName(), target.GetUid(), identity)
		}
	}
}

func (s *Service) getEnvironment(
	ctx context.Context,
	name string,
	uid string,
	identity auth.ServiceAccountPrincipal,
) (*discoverypb.EnvironmentDescriptor, error) {
	view, err := s.sandboxes.Get(ctx, name)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, status.Error(codes.NotFound, "environment not found")
		}
		slog.ErrorContext(ctx, "Environment discovery Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	if view.Identity.UID != uid || deleting(view.Resource) {
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
	return s.describe(ctx, view.Resource, execution, identity)
}

func deleting(object *api.Sandbox) bool {
	return object.DeletionTimestamp != nil && !object.DeletionTimestamp.IsZero()
}

func (s *Service) describe(
	ctx context.Context,
	object *api.Sandbox,
	execution sandboxreader.ServiceAccountIdentity,
	caller auth.ServiceAccountPrincipal,
) (*discoverypb.EnvironmentDescriptor, error) {
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
		resume = discoverypb.ResumeState_RESUME_STATE_NOT_AVAILABLE
	}
	target := lifecycle.Target{Namespace: object.Namespace, Name: object.Name, UID: string(object.UID)}
	eligibility, generation, err := s.lifecycle.Eligibility(ctx, target)
	if err != nil {
		return nil, err
	}
	if object.Status.OperatingMode == api.OperatingModeSuspended {
		wake, err := s.lifecycle.AssessAutomaticWake(ctx, target, lifecycle.ServiceAccount{
			Namespace: caller.Account.Namespace,
			Name:      caller.Account.Name,
			UID:       caller.ServiceAccountUID,
		})
		if err != nil {
			return nil, status.Error(codes.Unavailable, "automatic wake eligibility is unavailable")
		}
		if wake.GetState() == environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE {
			resume = discoverypb.ResumeState_RESUME_STATE_AVAILABLE
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
		ResumeEligibility:          eligibility,
		WakeGeneration:             generation,
	}
	if eligibility.GetState() == environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		descriptor.ResumeToken = lifecycle.NewResumeToken(target, generation)
	}
	if object.Status.Incarnation != nil {
		descriptor.RuntimeIncarnationUid = object.Status.Incarnation.UID
	}
	template, known := s.runtime.Templates[object.Spec.Template]
	if !known {
		return descriptor, nil
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
	return descriptor, nil
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
