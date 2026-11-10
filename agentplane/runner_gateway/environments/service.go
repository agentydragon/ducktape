// Package environments serves discovery and lifecycle operations for existing managed environments.
package environments

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/internal/view"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
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
	readers                sets.Set[auth.Account]
	managers               sets.Set[auth.Account]
	launchers              sets.Set[auth.Account]
	defaultPolicySets      []string
	collectionAuthInterval time.Duration
}

func New(
	namespace string,
	objects client.WithWatch,
	runtime runtimeconfig.Catalog,
	lifecycleManager *lifecycle.Manager,
	readers sets.Set[auth.Account],
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
	Managers          sets.Set[auth.Account]
	Launchers         sets.Set[auth.Account]
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

func serviceAccountReference(identity *auth.ServiceAccountPrincipal) auth.Account {
	if identity == nil {
		return auth.Account{}
	}
	account := identity.GetAccount()
	return auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()}
}

func caller(ctx context.Context) (*auth.ServiceAccountPrincipal, error) {
	identity, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return nil, err
	}
	if identity == nil || identity.GetAccount() == nil || identity.GetAccount().GetNamespace() == "" ||
		identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
		return nil, status.Error(codes.Unauthenticated, "effective ServiceAccount identity is incomplete")
	}
	return identity, nil
}

// broadReader applies only to direct credentials; delegated actors never lend their grants to a caller.
func (s *Service) broadReader(ctx context.Context, identity *auth.ServiceAccountPrincipal) bool {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false
	}
	if _, ok := admission.(auth.DirectAdmission); !ok {
		return false
	}
	ok := s.readers.Has(serviceAccountReference(identity))
	return ok
}

func callerMatches(caller *auth.ServiceAccountPrincipal, execution *sandboxreader.ServiceAccountIdentity) bool {
	return caller != nil && execution != nil && execution.GetAccount() != nil &&
		caller.GetAccount().GetNamespace() == execution.GetAccount().GetNamespace() &&
		caller.GetAccount().GetName() == execution.GetAccount().GetName() && caller.GetUid() == execution.GetUid()
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
	account := serviceAccountReference(identity)
	if account.Namespace != s.namespace {
		return response, nil
	}
	view, err := s.sandboxes.Get(ctx, account.Name)
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
		_, _, visible, err := s.revalidateDescriptor(ctx, descriptor, identity, false)
		if err != nil {
			slog.ErrorContext(ctx, "Environment discovery authorization recheck failed", "error", err)
			return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
		}
		if !visible {
			return response, nil
		}
		response.Items = append(response.Items, descriptor)
	}
	return response, nil
}

// listAll is used only by a direct, explicitly granted inventory reader. Its Kubernetes
// continuation may include names only that principal is authorized to discover.
func (s *Service) listAll(ctx context.Context, request *environmentpb.ListEnvironmentsRequest, identity *auth.ServiceAccountPrincipal) (*environmentpb.ListEnvironmentsResponse, error) {
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
		execution, _, err := s.sandboxes.ExecutionAccount(ctx, &sandboxreader.Identity{Namespace: object.Namespace, Name: object.Name, Uid: string(object.UID)})
		if err != nil {
			return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
		}
		descriptor, err := s.describe(ctx, object, execution, identity)
		if err != nil {
			return nil, err
		}
		_, _, visible, err := s.revalidateDescriptor(ctx, descriptor, identity, true)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
		}
		if !visible {
			continue
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
	descriptor, _, err := s.getEnvironment(ctx, request, identity)
	return descriptor, err
}

// getEnvironment returns both the full caller-authorized snapshot and the currently reported Pod
// name so a watch can observe that exact projection without following a stale incarnation.
func (s *Service) getEnvironment(
	ctx context.Context,
	request *environmentpb.GetEnvironmentRequest,
	identity *auth.ServiceAccountPrincipal,
) (*environmentpb.EnvironmentDescriptor, string, error) {
	target := request.GetTarget()
	if target == nil || target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" {
		return nil, "", status.Error(codes.InvalidArgument, "complete target handle is required")
	}
	if target.GetNamespace() != s.namespace {
		return nil, "", status.Error(codes.NotFound, "environment not found")
	}
	view, err := s.sandboxes.Get(ctx, target.GetName())
	if err != nil {
		if status.Code(err) == codes.NotFound {
			return nil, "", status.Error(codes.NotFound, "environment not found")
		}
		slog.ErrorContext(ctx, "Environment discovery Sandbox read failed", "error", err)
		return nil, "", status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	broad := s.broadReader(ctx, identity)
	if view.Identity.Uid != target.GetUid() || (deleting(view.Resource) && !broad) {
		return nil, "", status.Error(codes.NotFound, "environment not found")
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, view.Identity)
	if err != nil {
		slog.ErrorContext(ctx, "Environment discovery execution account read failed", "error", err)
		return nil, "", status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
	}
	if !broad && (!found || !callerMatches(identity, execution)) {
		return nil, "", status.Error(codes.NotFound, "environment not found")
	}
	descriptor, err := s.describe(ctx, view.Resource, execution, identity)
	if err != nil {
		return nil, "", err
	}
	if !found {
		descriptor.ExecutionAccount = nil
	}
	_, _, visible, err := s.revalidateDescriptor(ctx, descriptor, identity, broad)
	if err != nil {
		slog.ErrorContext(ctx, "Environment discovery authorization recheck failed", "error", err)
		return nil, "", status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
	}
	if !visible {
		return nil, "", status.Error(codes.NotFound, "environment not found")
	}
	podName := ""
	if incarnation := view.Resource.Status.Incarnation; incarnation != nil {
		podName = incarnation.Name
	}
	return descriptor, podName, nil
}

func deleting(object *api.Sandbox) bool {
	return object.DeletionTimestamp != nil && !object.DeletionTimestamp.IsZero()
}

// revalidateDescriptor fences the authorization and target identity again after assembling the
// Pod-inclusive projection. The extra reads close the window in which the Sandbox or its owner
// could change while the Pod was being fetched. Broad readers still receive the current account
// fact when available, but never a descriptor for a replaced Sandbox UID.
func (s *Service) revalidateDescriptor(
	ctx context.Context,
	descriptor *environmentpb.EnvironmentDescriptor,
	caller *auth.ServiceAccountPrincipal,
	broad bool,
) (*sandboxreader.ServiceAccountIdentity, bool, bool, error) {
	target := descriptor.GetTarget()
	if target == nil || target.GetNamespace() != s.namespace || target.GetName() == "" || target.GetUid() == "" {
		return nil, false, false, nil
	}
	current, err := s.sandboxes.Get(ctx, target.GetName())
	if status.Code(err) == codes.NotFound || (err == nil && current.Identity.Uid != target.GetUid()) {
		return nil, false, false, nil
	}
	if err != nil {
		return nil, false, false, err
	}
	if deleting(current.Resource) && !broad {
		return nil, false, false, nil
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, current.Identity)
	if err != nil {
		return nil, false, false, err
	}
	if !broad && (!found || !callerMatches(caller, execution)) {
		return nil, false, false, nil
	}
	if found {
		descriptor.ExecutionAccount = execution
	} else {
		descriptor.ExecutionAccount = nil
	}
	return execution, found, true, nil
}

func (s *Service) describe(
	ctx context.Context,
	object *api.Sandbox,
	execution *sandboxreader.ServiceAccountIdentity,
	caller *auth.ServiceAccountPrincipal,
) (*environmentpb.EnvironmentDescriptor, error) {
	var pod *corev1.Pod
	if incarnation := object.Status.Incarnation; incarnation != nil {
		candidate := &corev1.Pod{}
		err := s.objects.Get(ctx, types.NamespacedName{Namespace: object.Namespace, Name: incarnation.Name}, candidate)
		if err != nil && !apierrors.IsNotFound(err) {
			slog.ErrorContext(ctx, "Environment inventory Pod read failed", "error", err)
			return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
		}
		if err == nil {
			pod = candidate
		}
	}
	descriptor, err := view.View(object, pod)
	if err != nil {
		slog.ErrorContext(ctx, "Environment inventory projection failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	automaticWake := environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_UNSPECIFIED
	switch object.Status.OperatingMode {
	case api.OperatingModeRunning:
		automaticWake = environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_REQUIRED
	case api.OperatingModeSuspended:
		automaticWake = environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE
	}
	target := &lifecycle.Target{Namespace: object.Namespace, Name: object.Name, Uid: string(object.UID)}
	var eligibility *environmentlifecyclepb.ResumeEligibility
	generation := object.Spec.WakeGeneration
	if !deleting(object) {
		var err error
		eligibility, generation, err = s.lifecycle.Eligibility(ctx, target)
		if err != nil {
			return nil, err
		}
		if object.Status.OperatingMode == api.OperatingModeSuspended {
			wake, err := s.lifecycle.AssessAutomaticWake(ctx, target, lifecycle.AccountWakeCaller{Account: caller})
			if err != nil {
				return nil, status.Error(codes.Unavailable, "automatic wake eligibility is unavailable")
			}
			if wake.GetState() == environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE {
				automaticWake = environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_AVAILABLE
			}
		}
	}
	descriptor.ExecutionAccount = execution
	descriptor.Actions = &environmentpb.EnvironmentActions{
		AutomaticWake: automaticWake, ResumeEligibility: eligibility, WakeGeneration: generation,
	}
	if eligibility.GetState() == environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		descriptor.Actions.ResumeToken = lifecycle.NewResumeToken(target, generation)
	}
	descriptor.ObservedState.ObservedAt = timestamppb.New(time.Now().UTC())
	descriptor.Configuration.Workspace = &environmentpb.WorkspaceGuidance{Availability: environmentpb.Availability_AVAILABILITY_UNAVAILABLE}
	descriptor.Configuration.Instructions = &environmentpb.TextFact{Availability: environmentpb.Availability_AVAILABILITY_ABSENT}
	descriptor.Configuration.ConfigurationsAvailability = environmentpb.Availability_AVAILABILITY_UNAVAILABLE
	template, known := s.runtime.Templates[object.Spec.Template]
	if !known {
		return descriptor, nil
	}
	if template.WorkspaceRoot == "" {
		descriptor.Configuration.Workspace.Availability = environmentpb.Availability_AVAILABILITY_ABSENT
	} else if template.WorkspaceProvenance == runtimeconfig.ProvenanceDeploymentConfig {
		descriptor.Configuration.Workspace.Availability = environmentpb.Availability_AVAILABILITY_CONFIGURED
		descriptor.Configuration.Workspace.Root = template.WorkspaceRoot
		descriptor.Configuration.Workspace.Provenance = environmentpb.Provenance_PROVENANCE_DEPLOYMENT_CONFIG
		descriptor.Configuration.Workspace.RootEnforced = template.WorkspaceRootEnforced
		descriptor.Configuration.Workspace.CreatesMissingCwd = template.CreatesMissingCWD
	}
	descriptor.Configuration.Configurations = configuredChoices(s.runtime.Models)
	if len(descriptor.Configuration.Configurations) == 0 {
		descriptor.Configuration.ConfigurationsAvailability = environmentpb.Availability_AVAILABILITY_ABSENT
	} else {
		descriptor.Configuration.ConfigurationsAvailability = environmentpb.Availability_AVAILABILITY_CONFIGURED
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
