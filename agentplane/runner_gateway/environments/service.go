// Package environments serves discovery and lifecycle operations for existing managed environments.
package environments

import (
	"context"
	"errors"
	"log/slog"
	"slices"
	"strings"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/internal/view"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
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
	identityChanges        IdentityChangeGuard
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
		launchers:              management.Launchers,
		identityChanges:        management.IdentityChanges,
		collectionAuthInterval: auth.DefaultVerdictTTL,
	}, nil
}

// ManagementOptions grants mutation authority independently of inventory and resume grants.
type ManagementOptions struct {
	Managers        sets.Set[auth.Account]
	Launchers       sets.Set[auth.Account]
	IdentityChanges IdentityChangeGuard
}

// IdentityChangeGuard serializes a Sandbox patch with Dispatcher Session admission and rejects
// retained Sessions of a different Identity incarnation.
type IdentityChangeGuard interface {
	ChangeSandboxIdentity(context.Context, string, *identitypb.Identity, func() error) error
}

// Methods admits direct TokenReview-verified ServiceAccounts in the configured Resolver namespaces.
// List/Get also admit workload credentials naming an Identity. The composition root may enable
// delegated mode on selected methods; handlers recheck the effective subject against live ownership.
func Methods(resolver *auth.Resolver) auth.Methods {
	rule := auth.ReviewedServiceAccount(resolver)
	identityRead := auth.ReviewedServiceAccountOrIdentity(resolver)
	return auth.Methods{
		environmentpb.EnvironmentService_ListEnvironmentTemplates_FullMethodName:     rule,
		environmentpb.EnvironmentService_CreateEnvironment_FullMethodName:            rule,
		environmentpb.EnvironmentService_ListEnvironments_FullMethodName:             identityRead,
		environmentpb.EnvironmentService_WatchEnvironments_FullMethodName:            rule,
		environmentpb.EnvironmentService_GetEnvironment_FullMethodName:               identityRead,
		environmentpb.EnvironmentService_GetEnvironmentIdentityRecord_FullMethodName: rule,
		environmentpb.EnvironmentService_WatchEnvironment_FullMethodName:             rule,
		environmentpb.EnvironmentService_ResumeEnvironment_FullMethodName:            rule,
		environmentpb.EnvironmentService_SuspendEnvironment_FullMethodName:           rule,
		environmentpb.EnvironmentService_DeleteEnvironment_FullMethodName:            rule,
		environmentpb.EnvironmentService_SetEnvironmentWakeOnSend_FullMethodName:     rule,
		environmentpb.EnvironmentService_SetEnvironmentIdleSuspend_FullMethodName:    rule,
		environmentpb.EnvironmentService_SetEnvironmentIdentity_FullMethodName:       rule,
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

// environmentCaller is the effective subject for the read-only environment inventory methods.
// A delegated actor remains only the credential for authenticating the request; its own reader
// role never becomes authority for the effective Identity.
type environmentCaller struct {
	serviceAccount *auth.ServiceAccountPrincipal
	identity       *auth.Identity
}

func environmentSubject(ctx context.Context) (environmentCaller, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return environmentCaller{}, err
	}
	switch admitted := admission.(type) {
	case auth.DirectAdmission:
		return environmentCaller{serviceAccount: admitted.Caller}, nil
	case auth.DelegatedAdmission:
		return environmentCaller{serviceAccount: admitted.EffectiveCaller}, nil
	case auth.IdentityAdmission:
		identity := admitted.Caller.Identity
		return environmentCaller{identity: &identity}, nil
	case auth.DelegatedIdentityAdmission:
		identity := admitted.EffectiveCaller
		return environmentCaller{identity: &identity}, nil
	default:
		return environmentCaller{}, status.Error(codes.Unauthenticated, "environment caller is not admitted")
	}
}

func (caller environmentCaller) valid() bool {
	if caller.serviceAccount != nil {
		account := caller.serviceAccount.GetAccount()
		return account != nil && account.GetNamespace() != "" && account.GetName() != "" && caller.serviceAccount.GetUid() != ""
	}
	return caller.identity != nil && caller.identity.Name != "" && caller.identity.UID != ""
}

func (caller environmentCaller) isIdentity() bool { return caller.identity != nil }

func identityMatches(left *auth.Identity, right *identitypb.Identity) bool {
	return left != nil && right != nil && left.Name == right.GetName() && left.UID == right.GetUid()
}

func (s *Service) ownsSandbox(ctx context.Context, caller environmentCaller, target *sandboxreader.Identity) (bool, error) {
	if caller.identity != nil {
		acting, found, err := s.sandboxes.ExecutionIdentity(ctx, target)
		if err != nil || !found {
			return false, err
		}
		return identityMatches(caller.identity, acting), nil
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, target)
	if err != nil || !found {
		return false, err
	}
	return callerMatches(caller.serviceAccount, execution), nil
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
	identity, err := environmentSubject(ctx)
	if err != nil {
		return nil, err
	}
	if !identity.valid() {
		return nil, status.Error(codes.Unauthenticated, "effective environment identity is incomplete")
	}
	if identity.serviceAccount != nil && s.broadReader(ctx, identity.serviceAccount) {
		return s.listAll(ctx, request, identity.serviceAccount)
	}
	if identity.identity == nil && request.GetPageToken() != "" {
		return nil, status.Error(codes.InvalidArgument, "page token is not valid for this inventory")
	}
	if identity.identity != nil {
		return s.listIdentityEnvironments(ctx, request, identity)
	}
	response := &environmentpb.ListEnvironmentsResponse{Items: []*environmentpb.EnvironmentDescriptor{}}
	// Current Sandbox-owned execution accounts share the Sandbox's namespace and name. Read only
	// that candidate directly; a namespace-wide continuation token could reveal another Sandbox's
	// storage key even when its object is filtered out. The list contract keeps its page shape for
	// future account mappings without making today's lookup depend on foreign inventory.
	account := serviceAccountReference(identity.serviceAccount)
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
	if found && callerMatches(identity.serviceAccount, execution) {
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

// listIdentityEnvironments filters against the Sandbox's current named Identity and then fences
// every descriptor against the exact Sandbox UID and Identity UID again after projection. The
// page token is only a lexical cursor; it cannot select or expose a Sandbox outside this Identity's
// live binding.
func (s *Service) listIdentityEnvironments(
	ctx context.Context,
	request *environmentpb.ListEnvironmentsRequest,
	caller environmentCaller,
) (*environmentpb.ListEnvironmentsResponse, error) {
	var list api.SandboxList
	if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		return nil, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	slices.SortFunc(list.Items, func(left, right api.Sandbox) int {
		return strings.Compare(left.Name, right.Name)
	})
	response := &environmentpb.ListEnvironmentsResponse{Items: []*environmentpb.EnvironmentDescriptor{}}
	for i := range list.Items {
		object := &list.Items[i]
		if object.Name <= request.GetPageToken() || deleting(object) || object.Spec.Identity == nil || object.Spec.Identity.Name != caller.identity.Name {
			continue
		}
		target := &sandboxreader.Identity{Namespace: object.Namespace, Name: object.Name, Uid: string(object.UID)}
		owned, err := s.ownsSandbox(ctx, caller, target)
		if err != nil {
			slog.ErrorContext(ctx, "Environment inventory acting Identity read failed", "error", err)
			return nil, status.Error(codes.Unavailable, "Sandbox Identity is unavailable")
		}
		if !owned {
			continue
		}
		if len(response.Items) == int(request.GetLimit()) {
			response.NextPageToken = response.Items[len(response.Items)-1].GetTarget().GetName()
			break
		}
		descriptor, err := s.describe(ctx, object, nil, caller)
		if err != nil {
			return nil, err
		}
		_, _, visible, err := s.revalidateDescriptor(ctx, descriptor, caller, false)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "Sandbox Identity is unavailable")
		}
		if visible {
			response.Items = append(response.Items, descriptor)
		}
	}
	return response, nil
}

// listAll is used only by a direct, explicitly granted inventory reader. Its Kubernetes
// continuation may include names only that principal is authorized to discover.
func (s *Service) listAll(ctx context.Context, request *environmentpb.ListEnvironmentsRequest, identity *auth.ServiceAccountPrincipal) (*environmentpb.ListEnvironmentsResponse, error) {
	caller := environmentCaller{serviceAccount: identity}
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
		descriptor, err := s.describe(ctx, object, execution, caller)
		if err != nil {
			return nil, err
		}
		_, _, visible, err := s.revalidateDescriptor(ctx, descriptor, caller, true)
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
	identity, err := environmentSubject(ctx)
	if err != nil {
		return nil, err
	}
	if !identity.valid() {
		return nil, status.Error(codes.Unauthenticated, "effective environment identity is incomplete")
	}
	descriptor, _, err := s.getEnvironment(ctx, request, identity)
	return descriptor, err
}

func (s *Service) GetEnvironmentIdentityRecord(ctx context.Context, request *environmentpb.GetEnvironmentRequest) (*environmentpb.EnvironmentIdentityRecord, error) {
	if _, err := s.GetEnvironment(ctx, request); err != nil {
		return nil, err
	}
	target := request.GetTarget()
	record := &workloadapi.WorkloadCredential{}
	err := s.objects.Get(ctx, types.NamespacedName{Namespace: target.GetNamespace(), Name: target.GetName()}, record)
	if apierrors.IsNotFound(err) {
		return &environmentpb.EnvironmentIdentityRecord{}, nil
	}
	if err != nil {
		slog.ErrorContext(ctx, "Environment credential read failed", "error", err)
		return nil, status.Error(codes.Unavailable, "Environment credential is unavailable")
	}
	for _, owner := range record.OwnerReferences {
		if owner.APIVersion == api.GroupVersion.String() && owner.Kind == api.SandboxKind && owner.Name == target.GetName() && string(owner.UID) == target.GetUid() && owner.Controller != nil && *owner.Controller {
			if acting := record.Spec.Subject.Identity; acting != nil {
				return &environmentpb.EnvironmentIdentityRecord{Identity: &identitypb.Identity{Name: acting.Name, Uid: string(acting.UID)}}, nil
			}
			break
		}
	}
	return &environmentpb.EnvironmentIdentityRecord{}, nil
}

// getEnvironment returns both the full caller-authorized snapshot and the currently reported Pod
// name so a watch can observe that exact projection without following a stale incarnation.
func (s *Service) getEnvironment(
	ctx context.Context,
	request *environmentpb.GetEnvironmentRequest,
	identity environmentCaller,
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
	broad := identity.serviceAccount != nil && s.broadReader(ctx, identity.serviceAccount)
	if view.Identity.Uid != target.GetUid() || (deleting(view.Resource) && !broad) {
		return nil, "", status.Error(codes.NotFound, "environment not found")
	}
	var execution *sandboxreader.ServiceAccountIdentity
	found := false
	if identity.serviceAccount != nil {
		execution, found, err = s.sandboxes.ExecutionAccount(ctx, view.Identity)
		if err != nil {
			slog.ErrorContext(ctx, "Environment discovery execution account read failed", "error", err)
			return nil, "", status.Error(codes.Unavailable, "Sandbox execution account is unavailable")
		}
	}
	if !broad {
		if identity.identity != nil {
			owned, err := s.ownsSandbox(ctx, identity, view.Identity)
			if err != nil {
				slog.ErrorContext(ctx, "Environment discovery owner read failed", "error", err)
				return nil, "", status.Error(codes.Unavailable, "Sandbox Identity is unavailable")
			}
			if !owned {
				return nil, "", status.Error(codes.NotFound, "environment not found")
			}
		} else if !found || !callerMatches(identity.serviceAccount, execution) {
			return nil, "", status.Error(codes.NotFound, "environment not found")
		}
	}
	descriptor, err := s.describe(ctx, view.Resource, execution, identity)
	if err != nil {
		return nil, "", err
	}
	if !found || identity.isIdentity() {
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
	caller environmentCaller,
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
	var execution *sandboxreader.ServiceAccountIdentity
	found := false
	if caller.serviceAccount != nil || broad {
		execution, found, err = s.sandboxes.ExecutionAccount(ctx, current.Identity)
		if err != nil {
			return nil, false, false, err
		}
	}
	if !broad {
		if caller.identity != nil {
			owned, err := s.ownsSandbox(ctx, caller, current.Identity)
			if err != nil {
				return nil, false, false, err
			}
			if !owned {
				return nil, false, false, nil
			}
		} else if !found || !callerMatches(caller.serviceAccount, execution) {
			return nil, false, false, nil
		}
	}
	if found && !caller.isIdentity() {
		descriptor.ExecutionAccount = execution
	} else {
		descriptor.ExecutionAccount = nil
	}
	descriptor.IdentityName = nil
	if current.Resource.Spec.Identity != nil {
		descriptor.IdentityName = &current.Resource.Spec.Identity.Name
	}
	return execution, found, true, nil
}

func (s *Service) describe(
	ctx context.Context,
	object *api.Sandbox,
	execution *sandboxreader.ServiceAccountIdentity,
	caller environmentCaller,
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
	if !caller.isIdentity() && !deleting(object) {
		var err error
		eligibility, generation, err = s.lifecycle.Eligibility(ctx, target)
		if err != nil {
			return nil, err
		}
		if object.Status.OperatingMode == api.OperatingModeSuspended {
			wake, err := s.lifecycle.AssessAutomaticWake(ctx, target, lifecycle.AccountWakeCaller{Account: caller.serviceAccount})
			if err != nil {
				return nil, status.Error(codes.Unavailable, "automatic wake eligibility is unavailable")
			}
			if wake.GetState() == environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE {
				automaticWake = environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_AVAILABLE
			}
		}
	}
	if caller.isIdentity() {
		automaticWake = environmentpb.AutomaticWakeState_AUTOMATIC_WAKE_STATE_NOT_AVAILABLE
		descriptor.ExecutionAccount = nil
		generation = 0
	} else {
		descriptor.ExecutionAccount = execution
	}
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
