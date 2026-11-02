// Package environments serves discovery and lifecycle operations for existing managed environments.
package environments

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"time"

	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
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

// Service serves environment inventory and explicit lifecycle requests in one namespace. The client is uncached so every page and exact
// lookup starts from a current API-server view; ExecutionAccount rereads each candidate's UID and
// controller-owned account before it is returned.
type Service struct {
	environmentpb.UnimplementedEnvironmentServiceServer

	namespace              string
	objects                client.WithWatch
	sandboxes              *sandboxreader.Reader
	runtime                runtimeconfig.Catalog
	lifecycle              *lifecycle.Manager
	readers                map[auth.Account]struct{}
	managers               map[auth.Account]struct{}
	launchers              map[auth.Account]struct{}
	defaultPolicySets      []string
	collectionAuthInterval time.Duration
}

func New(
	namespace string,
	objects client.WithWatch,
	runtime runtimeconfig.Catalog,
	lifecycleManager *lifecycle.Manager,
	readers map[auth.Account]struct{},
	management ManagementOptions,
) (*Service, error) {
	if namespace == "" || objects == nil || lifecycleManager == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes watch client, and lifecycle manager are required")
	}
	sandboxes, err := sandboxreader.New(namespace, objects)
	if err != nil {
		return nil, err
	}
	return &Service{
		namespace: namespace, objects: objects, sandboxes: sandboxes, runtime: runtime,
		lifecycle: lifecycleManager, readers: readers, managers: management.Managers,
		launchers: management.Launchers, defaultPolicySets: slices.Clone(management.DefaultPolicySets),
		collectionAuthInterval: auth.DefaultVerdictTTL,
	}, nil
}

// ManagementOptions grants mutation authority independently of inventory and resume grants.
type ManagementOptions struct {
	Managers          map[auth.Account]struct{}
	Launchers         map[auth.Account]struct{}
	DefaultPolicySets []string
}

// Methods admits direct TokenReview-verified ServiceAccounts in the configured Resolver namespaces.
// The composition root may enable the shared delegated mode on selected methods; object-level
// authority is decided by the handler using the effective caller's live account UID.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	return auth.Methods{
		environmentpb.EnvironmentService_ListEnvironmentTemplates_FullMethodName:  rule,
		environmentpb.EnvironmentService_CreateEnvironment_FullMethodName:         rule,
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName:          rule,
		environmentpb.EnvironmentService_WatchEnvironments_FullMethodName:         rule,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName:            rule,
		environmentpb.EnvironmentService_WatchEnvironment_FullMethodName:          rule,
		environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName:         rule,
		environmentpb.EnvironmentService_SuspendEnvironment_FullMethodName:        rule,
		environmentpb.EnvironmentService_DeleteEnvironment_FullMethodName:         rule,
		environmentpb.EnvironmentService_SetEnvironmentWakeOnSend_FullMethodName:  rule,
		environmentpb.EnvironmentService_SetEnvironmentIdleSuspend_FullMethodName: rule,
	}
}

// ResumeEnvironment rechecks lifecycle authority and requests desired running state.
// Acceptance does not imply readiness and never starts a Session or replays execution.
func (s *Service) ResumeEnvironment(ctx context.Context, request *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error) {
	return s.lifecycle.ResumeEnvironment(ctx, request)
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

// broadReader applies only to direct credentials; delegated actors never lend their grants to a caller.
func (s *Service) broadReader(ctx context.Context, identity auth.ServiceAccountPrincipal) bool {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false
	}
	if _, ok := admission.(auth.DirectAdmission); !ok {
		return false
	}
	_, ok := s.readers[identity.Account]
	return ok
}

func callerMatches(caller auth.ServiceAccountPrincipal, execution sandboxreader.ServiceAccountIdentity) bool {
	return caller.Account.Namespace == execution.Namespace && caller.Account.Name == execution.Name &&
		caller.ServiceAccountUID == execution.UID
}

func (s *Service) ListEnvironments(ctx context.Context, request *environmentpb.ListEnvironmentsRequest) (*environmentpb.ListEnvironmentsResponse, error) {
	if request == nil || request.GetLimit() < 1 || request.GetLimit() > maxLimit {
		return nil, status.Errorf(codes.InvalidArgument, "limit must be between 1 and %d", maxLimit)
	}
	identity, err := caller(ctx)
	if err != nil {
		return nil, err
	}
	if s.broadReader(ctx, identity) {
		return s.listAll(ctx, request, identity)
	}
	if request.GetPageToken() != "" {
		return nil, status.Error(codes.InvalidArgument, "page token is not valid for this inventory")
	}
	response := &environmentpb.ListEnvironmentsResponse{Items: []*environmentpb.EnvironmentDescriptor{}}
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
		slog.ErrorContext(ctx, "Environment inventory Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	if deleting(view.Resource) {
		return response, nil
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, view.Identity)
	if err != nil {
		slog.ErrorContext(ctx, "Environment inventory execution account read failed", "error", err)
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

// listAll is used only by a direct, explicitly granted inventory reader. Its Kubernetes
// continuation may include names only that principal is authorized to discover.
func (s *Service) listAll(ctx context.Context, request *environmentpb.ListEnvironmentsRequest, identity auth.ServiceAccountPrincipal) (*environmentpb.ListEnvironmentsResponse, error) {
	var list api.SandboxList
	opts := []client.ListOption{client.InNamespace(s.namespace), client.Limit(int64(request.GetLimit()))}
	if request.GetPageToken() != "" {
		opts = append(opts, client.Continue(request.GetPageToken()))
	}
	if err := s.objects.List(ctx, &list, opts...); err != nil {
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	response := &environmentpb.ListEnvironmentsResponse{NextPageToken: list.Continue}
	for i := range list.Items {
		object := &list.Items[i]
		execution, found, err := s.sandboxes.ExecutionAccount(ctx, sandboxreader.Identity{Namespace: object.Namespace, Name: object.Name, UID: string(object.UID)})
		if err != nil {
			return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
		}
		descriptor, err := s.describe(ctx, object, execution, identity)
		if err != nil {
			return nil, err
		}
		if !found {
			descriptor.ExecutionAccount = nil
		}
		response.Items = append(response.Items, descriptor)
	}
	return response, nil
}

func (s *Service) GetEnvironment(ctx context.Context, request *environmentpb.GetEnvironmentRequest) (*environmentpb.EnvironmentDescriptor, error) {
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
	return s.getEnvironment(ctx, target.GetName(), target.GetUid(), identity)
}

func (s *Service) getEnvironment(
	ctx context.Context,
	name string,
	uid string,
	identity auth.ServiceAccountPrincipal,
) (*environmentpb.EnvironmentDescriptor, error) {
	view, err := s.sandboxes.Get(ctx, name)
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, status.Error(codes.NotFound, "environment not found")
		}
		slog.ErrorContext(ctx, "Environment inventory Sandbox read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	if view.Identity.UID != uid || (deleting(view.Resource) && !s.broadReader(ctx, identity)) {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, view.Identity)
	if err != nil {
		slog.ErrorContext(ctx, "Environment inventory execution account read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
	}
	if !s.broadReader(ctx, identity) && (!found || !callerMatches(identity, execution)) {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	descriptor, err := s.describe(ctx, view.Resource, execution, identity)
	if err != nil {
		return nil, err
	}
	if !found {
		descriptor.ExecutionAccount = nil
	}
	return descriptor, nil
}

func deleting(object *api.Sandbox) bool {
	return object.DeletionTimestamp != nil && !object.DeletionTimestamp.IsZero()
}

func (s *Service) describe(
	ctx context.Context,
	object *api.Sandbox,
	execution sandboxreader.ServiceAccountIdentity,
	caller auth.ServiceAccountPrincipal,
) (*environmentpb.EnvironmentDescriptor, error) {
	condition := meta.FindStatusCondition(object.Status.Conditions, api.ConditionReady)
	readiness := &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_NOT_READY}
	if condition == nil {
		readiness.Reason = "ReadyConditionUnavailable"
	} else {
		readiness.Detail = condition.Message
		readiness.Reason = condition.Reason
		if condition.Status == metav1.ConditionTrue {
			readiness.State = environmentpb.ReadinessState_READINESS_STATE_READY
		}
	}
	mode := environmentpb.OperatingMode_OPERATING_MODE_UNSPECIFIED
	resume := environmentpb.ResumeState_RESUME_STATE_UNSPECIFIED
	switch object.Status.OperatingMode {
	case api.OperatingModeRunning:
		mode = environmentpb.OperatingMode_OPERATING_MODE_RUNNING
		resume = environmentpb.ResumeState_RESUME_STATE_NOT_REQUIRED
	case api.OperatingModeSuspended:
		mode = environmentpb.OperatingMode_OPERATING_MODE_SUSPENDED
		resume = environmentpb.ResumeState_RESUME_STATE_NOT_AVAILABLE
	}
	target := lifecycle.Target{Namespace: object.Namespace, Name: object.Name, UID: string(object.UID)}
	var eligibility *environmentlifecyclepb.ResumeEligibility
	generation := object.Spec.WakeGeneration
	if !deleting(object) {
		var err error
		eligibility, generation, err = s.lifecycle.Eligibility(ctx, target)
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
				resume = environmentpb.ResumeState_RESUME_STATE_AVAILABLE
			}
		}
	}
	descriptor := &environmentpb.EnvironmentDescriptor{
		Target: &environmentpb.TargetHandle{Namespace: object.Namespace, Name: object.Name, Uid: string(object.UID)},
		ExecutionAccount: &environmentpb.AccountHandle{
			Namespace: execution.Namespace, Name: execution.Name, Uid: execution.UID,
		},
		Readiness:                  readiness,
		Workspace:                  &environmentpb.WorkspaceGuidance{Availability: environmentpb.Availability_AVAILABILITY_UNAVAILABLE},
		Instructions:               &environmentpb.TextFact{Availability: environmentpb.Availability_AVAILABILITY_ABSENT},
		ConfigurationsAvailability: environmentpb.Availability_AVAILABILITY_UNAVAILABLE,
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
		descriptor.Workspace.Availability = environmentpb.Availability_AVAILABILITY_ABSENT
	} else if template.WorkspaceProvenance == runtimeconfig.ProvenanceDeploymentConfig {
		descriptor.Workspace.Availability = environmentpb.Availability_AVAILABILITY_CONFIGURED
		descriptor.Workspace.Root = template.WorkspaceRoot
		descriptor.Workspace.Provenance = environmentpb.Provenance_PROVENANCE_DEPLOYMENT_CONFIG
		descriptor.Workspace.RootEnforced = template.WorkspaceRootEnforced
		descriptor.Workspace.CreatesMissingCwd = template.CreatesMissingCWD
	}
	descriptor.Configurations = configuredChoices(s.runtime.Models)
	if len(descriptor.Configurations) == 0 {
		descriptor.ConfigurationsAvailability = environmentpb.Availability_AVAILABILITY_ABSENT
	} else {
		descriptor.ConfigurationsAvailability = environmentpb.Availability_AVAILABILITY_CONFIGURED
	}
	return descriptor, nil
}

func configuredChoices(catalog runtimeconfig.ModelCatalog) []*environmentpb.RuntimeConfiguration {
	models := make(map[string]runtimeconfig.ModelOption, len(catalog.Models))
	for _, model := range catalog.Models {
		models[model.Model] = model
	}
	harnesses := make([]string, 0, len(catalog.Harnesses))
	for harness := range catalog.Harnesses {
		harnesses = append(harnesses, harness)
	}
	slices.Sort(harnesses)
	var result []*environmentpb.RuntimeConfiguration
	for _, harness := range harnesses {
		for _, modelID := range catalog.Harnesses[harness] {
			model, ok := models[modelID]
			if !ok {
				continue
			}
			result = append(result, &environmentpb.RuntimeConfiguration{
				Harness: harness, Model: model.Model, DisplayName: model.DisplayName,
				ReasoningEfforts: stringsOrEmpty(model.ReasoningEfforts),
				Provenance:       environmentpb.Provenance_PROVENANCE_DEPLOYMENT_CONFIG,
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

var _ environmentpb.EnvironmentServiceServer = (*Service)(nil)
