package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"math"
	"slices"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/sets"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const maxPatchAttempts = 5

var (
	errInvalidTarget = errors.New("exact Sandbox target is required")
	errInvalidCaller = errors.New("admitted caller identity is incomplete")
)

// Options configure shared lifecycle authorization and guarded writes. WakeServiceCallers are
// automatic-wake brokers; they do not authorize explicit RPC calls or override a requested stop.
// OperatorServiceCallers are a separate explicit-resume grant. EnvironmentRules and PolicyReady
// provide Identity explicit-resume authority; either being absent fails closed for Identity callers.
// Identities resolves the live incarnation of Identities in Namespace: Sandbox execution principals and
// Identity callers.
type Options struct {
	Namespace              string
	Objects                client.Client
	Identities             auth.IdentityUIDReader
	WakeServiceCallers     []string
	OperatorServiceCallers []string
	EnvironmentRules       *reader.Index[access.Rule]
	PolicyReady            func() error
}

// Policy shares permission checks and optimistic Sandbox writes between the explicit-resume
// handler and Dispatcher wake executor. It owns neither RPC handling nor wake budgets.
// Objects must be an uncached API client so every authorization and patch uses
// current target, caller and execution-principal facts. Identity grants use the policy reader
// observation and readiness contract.
type Policy struct {
	namespace        string
	objects          client.Client
	identities       auth.IdentityUIDReader
	sandboxes        *sandboxreader.Reader
	wakeServices     sets.Set[string]
	operatorServices sets.Set[string]
	environmentRules *reader.Index[access.Rule]
	policyReady      func() error
}

// NewPolicy requires an uncached Kubernetes client; policy views remain local to each consumer.
func NewPolicy(options Options) (*Policy, error) {
	if options.Namespace == "" || options.Objects == nil || options.Identities == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes client and Identity reader are required")
	}
	if err := auth.ValidateServiceCallers(options.WakeServiceCallers); err != nil {
		return nil, fmt.Errorf("wake service callers: %w", err)
	}
	if err := auth.ValidateServiceCallers(options.OperatorServiceCallers); err != nil {
		return nil, fmt.Errorf("operator service callers: %w", err)
	}
	sandboxes, err := sandboxreader.New(options.Namespace, options.Objects, options.Identities)
	if err != nil {
		return nil, err
	}
	return &Policy{
		namespace: options.Namespace, objects: options.Objects, identities: options.Identities, sandboxes: sandboxes,
		environmentRules: options.EnvironmentRules, policyReady: options.PolicyReady,
		wakeServices: sets.New(options.WakeServiceCallers...), operatorServices: sets.New(options.OperatorServiceCallers...),
	}, nil
}

// Eligibility reports the current explicit-resume authority and the generation from the same
// uncached Sandbox read. Trusted operator eligibility is reported only for direct Pod-bound calls.
func (s *Policy) Eligibility(ctx context.Context, target *Target) (*environmentlifecyclepb.ResumeEligibility, int64, error) {
	caller, operator, err := s.AdmittedResumeCaller(ctx)
	if err != nil {
		return nil, 0, err
	}
	view, err := s.ReadTarget(ctx, target)
	if err != nil {
		return nil, 0, err
	}
	eligibility, err := s.EligibilityFor(ctx, view, caller, operator)
	if err != nil {
		return nil, 0, err
	}
	return eligibility, view.Spec.WakeGeneration, nil
}

// AssessAutomaticWake reports current automatic-wake policy without reserving budget or changing
// Sandbox state. Discovery uses it to describe whether a suspended target may wake on delivery.
func (s *Policy) AssessAutomaticWake(ctx context.Context, target *Target, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	if !ValidTarget(target) || target.Namespace != s.namespace {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	if !validWakeCaller(caller) {
		return nil, errInvalidCaller
	}
	view, err := s.ReadTarget(ctx, target)
	if err != nil {
		return assessmentFromError(err), nil
	}
	result, _ := s.automaticWakeAssessment(ctx, view, caller)
	return result, nil
}

func (s *Policy) applyResume(
	ctx context.Context,
	target *Target,
	caller WakeCaller,
	resumeToken string,
	operator bool,
	automatic bool,
	reserve func(context.Context, int64) (time.Duration, error),
) *environmentlifecyclepb.WakeAssessment {
	for attempt := 0; attempt < maxPatchAttempts; attempt++ {
		view, err := s.ReadTarget(ctx, target)
		if err != nil {
			return assessmentFromError(err)
		}
		var eligibility *environmentlifecyclepb.ResumeEligibility
		if !automatic {
			eligibility, err = s.EligibilityFor(ctx, view, caller, operator)
			if err != nil {
				return failureAssessment(codes.Unavailable, "caller or execution account state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration)
			}
			if eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
				return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, wakeReasonForEligibility(eligibility), view.Spec.WakeGeneration)
			}
		}
		if automatic {
			wakePolicy, permitted := s.automaticWakeAssessment(ctx, view, caller)
			if !permitted {
				return wakePolicy
			}
		}
		if resumeToken != "" && resumeToken != NewResumeToken(target, view.Spec.WakeGeneration) {
			return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, view.Spec.WakeGeneration)
		}
		if view.Spec.WakeGeneration == math.MaxInt64 {
			return failureAssessment(codes.FailedPrecondition, "wake generation cannot be incremented", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration)
		}
		if stopping := stoppingAssessment(view); stopping != nil {
			return stopping
		}

		idleSuspended := view.Status.StopStage == sandboxapi.StopStageStopped && idleSuspensionAtCurrentGeneration(view)
		if !idleSuspended {
			if view.Spec.Running {
				return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_NEEDED, environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED, view.Spec.WakeGeneration)
			}
			if !operator {
				return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, view.Spec.WakeGeneration)
			}
		}

		nextGeneration := view.Spec.WakeGeneration + 1
		if automatic {
			delay, err := reserve(ctx, nextGeneration)
			if err != nil {
				return failureAssessment(codes.Unavailable, "shared wake budget is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration)
			}
			if delay > 0 {
				return rateLimited(delay, view.Spec.WakeGeneration)
			}
		}
		updated := view.DeepCopy()
		updated.Spec.Running = true
		updated.Spec.WakeGeneration = nextGeneration
		err = s.objects.Patch(ctx, updated, client.MergeFromWithOptions(view, client.MergeFromWithOptimisticLock{}))
		if apierrors.IsConflict(err) {
			continue
		}
		if err != nil {
			return unknownAssessment(view.Spec.WakeGeneration)
		}
		reason := environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED
		if automatic && s.isWakeBroker(caller) && (eligibility == nil || eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE) {
			reason = environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER
		}
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED, reason, nextGeneration)
	}
	return failureAssessment(codes.Aborted, "Sandbox changed during all guarded resume attempts", environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, -1)
}

func (s *Policy) automaticWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, bool) {
	generation := view.Spec.WakeGeneration
	if stopping := stoppingAssessment(view); stopping != nil {
		return stopping, false
	}
	if requestedStop(view) {
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, generation), false
	}
	if !view.Spec.WakeOnSend {
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED, environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED, generation), false
	}
	switch caller := caller.(type) {
	case AccountWakeCaller:
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
	case IdentityWakeCaller:
		return s.identityWakeAssessment(ctx, view, caller.Identity)
	case ServiceWakeCaller:
		if !s.wakeServices.Has(caller.SPIFFEID) {
			return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
		}
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE, environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER, generation), true
	default:
		panic(fmt.Sprintf("unknown wake caller %T", caller))
	}
}

func stoppingAssessment(view *sandboxapi.Sandbox) *environmentlifecyclepb.WakeAssessment {
	generation := view.Spec.WakeGeneration
	if view.Status.StopStage == sandboxapi.StopStageDeleting || view.DeletionTimestamp != nil {
		return failureAssessment(codes.NotFound, "Sandbox is deleting", environmentlifecyclepb.WakeReason_WAKE_REASON_DELETING, generation)
	}
	if view.Status.StopStage == sandboxapi.StopStageQuiescing {
		return failureAssessment(codes.FailedPrecondition, "Sandbox is quiescing", environmentlifecyclepb.WakeReason_WAKE_REASON_QUIESCING, generation)
	}
	if idleSuspensionAtCurrentGeneration(view) && view.Status.StopStage != sandboxapi.StopStageStopped {
		return failureAssessment(codes.FailedPrecondition, "Sandbox has not completed its idle suspension", environmentlifecyclepb.WakeReason_WAKE_REASON_QUIESCING, generation)
	}
	return nil
}

func idleSuspensionAtCurrentGeneration(view *sandboxapi.Sandbox) bool {
	return view.Spec.Running && view.Status.OperatingMode == sandboxapi.OperatingModeSuspended &&
		view.Status.SuspendedBy != nil && *view.Status.SuspendedBy == sandboxapi.SuspendedByIdle &&
		view.Status.SuspendedAtWakeGeneration != nil && *view.Status.SuspendedAtWakeGeneration == view.Spec.WakeGeneration
}

// identityWakeAssessment authorizes the Sandbox's currently bound Identity under the existing
// resume-policy opt-in. It never resolves or falls back to the Sandbox's same-name ServiceAccount.
func (s *Policy) identityWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller *identitypb.Identity) (*environmentlifecyclepb.WakeAssessment, bool) {
	generation := view.Spec.WakeGeneration
	named, found, err := s.sandboxes.ExecutionIdentity(ctx, &sandboxreader.Identity{
		Namespace: view.Namespace, Name: view.Name, Uid: string(view.UID),
	})
	if err != nil {
		return failureAssessment(codes.Unavailable, "execution Identity state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, generation), false
	}
	switch {
	case !found || named == nil || named.GetName() != caller.GetName():
		// The Sandbox acts as no live Identity, or as another: the caller is not its execution principal.
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
	case named.GetUid() != caller.GetUid():
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED, generation), false
	case view.Spec.ResumePolicy == nil || !view.Spec.ResumePolicy.AllowOwnServiceAccount:
		return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED, generation), false
	}
	return AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE, environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED, generation), true
}

// EligibilityFor evaluates explicit authority against this current Sandbox view and live principals.
func (s *Policy) EligibilityFor(ctx context.Context, view *sandboxapi.Sandbox, caller WakeCaller, operator bool) (*environmentlifecyclepb.ResumeEligibility, error) {
	switch caller := caller.(type) {
	case AccountWakeCaller:
		live, err := s.callerAccountCurrent(ctx, caller.Account)
		if err != nil {
			return nil, status.Error(codes.Unavailable, "caller ServiceAccount state is unavailable")
		}
		if !live {
			return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED), nil
		}
		if requestedStop(view) {
			return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP), nil
		}
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
	case IdentityWakeCaller:
		return s.identityEligibility(ctx, view, caller.Identity)
	case ServiceWakeCaller:
		if operator {
			return allowed(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_TRUSTED_OPERATOR), nil
		}
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
	default:
		return nil, errInvalidCaller
	}
}

func (s *Policy) identityEligibility(ctx context.Context, view *sandboxapi.Sandbox, caller *identitypb.Identity) (*environmentlifecyclepb.ResumeEligibility, error) {
	if !validIdentity(caller) {
		return nil, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
	}
	live, err := s.callerIdentityCurrent(ctx, caller)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "caller Identity state is unavailable")
	}
	if !live {
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED), nil
	}
	if s.environmentRules == nil || s.policyReady == nil {
		return nil, status.Error(codes.Unavailable, "environment policy is unavailable")
	}
	if err := s.policyReady(); err != nil {
		return nil, status.Error(codes.Unavailable, "environment policy is unavailable")
	}
	policyCaller := reader.Identity{Name: caller.GetName(), UID: types.UID(caller.GetUid())}
	granted := access.Allowed(s.environmentRules, policyCaller, "resume", view)
	if granted && selectorRequiresLiveBoundIdentity(s.environmentRules, policyCaller, view) {
		bound, found, err := s.sandboxes.ExecutionIdentity(ctx, &sandboxreader.Identity{
			Namespace: view.Namespace, Name: view.Name, Uid: string(view.UID),
		})
		if err != nil {
			return nil, status.Error(codes.Unavailable, "target Identity state is unavailable")
		}
		granted = found && bound != nil && view.Spec.Identity != nil && bound.GetName() == view.Spec.Identity.Name
	}
	if requestedStop(view) {
		if !granted {
			return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
		}
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP), nil
	}
	if granted {
		return allowed(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_IDENTITY_GRANT), nil
	}
	return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
}

func selectorRequiresLiveBoundIdentity(index *reader.Index[access.Rule], caller reader.Identity, sandbox *sandboxapi.Sandbox) bool {
	matchedBoundIdentity, matchedWithoutBoundIdentity := false, false
	for _, grant := range index.Grants(caller) {
		for _, set := range grant.Sets {
			for _, numbered := range set.Rules {
				if !slices.Contains(numbered.Rule.Operations, "resume") {
					continue
				}
				for _, selector := range numbered.Rule.Environments {
					if !selectorMatches(sandbox, caller, selector) {
						continue
					}
					if selector.BoundIdentity == nil {
						matchedWithoutBoundIdentity = true
					} else {
						matchedBoundIdentity = true
					}
				}
			}
		}
	}
	return matchedBoundIdentity && !matchedWithoutBoundIdentity
}

func selectorMatches(sandbox *sandboxapi.Sandbox, caller reader.Identity, selector policyapi.EnvironmentSelector) bool {
	if selector.Sandbox != nil && (selector.Sandbox.Name != sandbox.Name || (selector.Sandbox.UID != "" && selector.Sandbox.UID != sandbox.UID)) {
		return false
	}
	if selector.BoundIdentity == nil {
		return true
	}
	if sandbox.Spec.Identity == nil {
		return false
	}
	if selector.BoundIdentity.Self {
		return sandbox.Spec.Identity.Name == caller.Name
	}
	return sandbox.Spec.Identity.Name == selector.BoundIdentity.Name
}

func requestedStop(view *sandboxapi.Sandbox) bool {
	return !view.Spec.Running || (view.Status.SuspendedBy != nil && *view.Status.SuspendedBy == sandboxapi.SuspendedByRequested)
}

// isWakeBroker reports whether caller is a configured automatic-wake broker.
func (s *Policy) isWakeBroker(caller WakeCaller) bool {
	service, ok := caller.(ServiceWakeCaller)
	return ok && s.wakeServices.Has(service.SPIFFEID)
}

func (s *Policy) explicitOperator(ctx context.Context, caller WakeCaller) bool {
	service, ok := caller.(ServiceWakeCaller)
	if !ok {
		return false
	}
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false
	}
	admitted, ok := admission.(auth.ServiceAdmission)
	return ok && admitted.Caller.SPIFFEID == service.SPIFFEID && s.operatorServices.Has(service.SPIFFEID)
}

// AdmittedResumeCaller preserves the effective Identity and a service operator's provenance.
func (s *Policy) AdmittedResumeCaller(ctx context.Context) (WakeCaller, bool, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
	}
	switch caller := admission.(type) {
	case auth.DirectAdmission:
		if !validServiceAccount(caller.Caller) {
			return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
		}
		return AccountWakeCaller{Account: caller.Caller}, false, nil
	case auth.ServiceAdmission:
		if err := auth.ValidateServiceCallers([]string{caller.Caller.SPIFFEID}); err != nil {
			return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
		}
		wakeCaller := ServiceWakeCaller{SPIFFEID: caller.Caller.SPIFFEID}
		return wakeCaller, s.explicitOperator(ctx, wakeCaller), nil
	case auth.IdentityAdmission:
		identity := &identitypb.Identity{Name: caller.Caller.Identity.Name, Uid: caller.Caller.Identity.UID}
		if !validIdentity(identity) {
			return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
		}
		return IdentityWakeCaller{Identity: identity}, false, nil
	case auth.DelegatedServiceIdentityAdmission:
		identity := &identitypb.Identity{Name: caller.EffectiveCaller.Name, Uid: caller.EffectiveCaller.UID}
		if !validIdentity(identity) {
			return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
		}
		return IdentityWakeCaller{Identity: identity}, false, nil
	default:
		return nil, false, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
	}
}

func (s *Policy) callerAccountCurrent(ctx context.Context, caller *ServiceAccountIdentity) (bool, error) {
	if !validServiceAccount(caller) {
		return false, nil
	}
	var account corev1.ServiceAccount
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: caller.GetAccount().GetNamespace(), Name: caller.GetAccount().GetName()}, &account); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return account.UID != "" && string(account.UID) == caller.GetUid() && account.DeletionTimestamp == nil, nil
}

func (s *Policy) callerIdentityCurrent(ctx context.Context, caller *identitypb.Identity) (bool, error) {
	if !validIdentity(caller) {
		return false, nil
	}
	uid, found, err := s.identities.GetIdentityUID(ctx, caller.GetName())
	if err != nil {
		return false, err
	}
	return found && uid != "" && uid == caller.GetUid(), nil
}

// ResumeDenial hides a foreign target when the denied Identity cannot read it.
func (s *Policy) ResumeDenial(ctx context.Context, target *Target, caller WakeCaller, eligibility *environmentlifecyclepb.ResumeEligibility) error {
	identityCaller, isIdentity := caller.(IdentityWakeCaller)
	if isIdentity {
		readable, err := s.identityCanReadTarget(ctx, target, identityCaller.Identity)
		if err != nil {
			return err
		}
		if !readable {
			return status.Error(codes.NotFound, "Sandbox target not found")
		}
	}
	return permissionDenied(eligibility)
}

func (s *Policy) identityCanReadTarget(ctx context.Context, target *Target, caller *identitypb.Identity) (bool, error) {
	bound, found, err := s.sandboxes.ExecutionIdentity(ctx, &sandboxreader.Identity{
		Namespace: target.Namespace, Name: target.Name, Uid: target.Uid,
	})
	if err != nil {
		return false, status.Error(codes.Unavailable, "target Identity state is unavailable")
	}
	return found && bound != nil && bound.GetName() == caller.GetName() && bound.GetUid() == caller.GetUid(), nil
}

// ReadTarget reads one exact live UID from the configured namespace without a cache.
func (s *Policy) ReadTarget(ctx context.Context, target *Target) (*sandboxapi.Sandbox, error) {
	if !ValidTarget(target) || target.Namespace != s.namespace {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	}
	var object sandboxapi.Sandbox
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: target.Namespace, Name: target.Name}, &object); apierrors.IsNotFound(err) {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	} else if err != nil {
		return nil, status.Error(codes.Unavailable, "Sandbox state is unavailable")
	}
	if string(object.UID) != target.Uid || object.DeletionTimestamp != nil {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	}
	return &object, nil
}

// ValidTarget requires namespace, name and UID, so name reuse cannot retarget a request.
func ValidTarget(target *Target) bool {
	return target != nil && target.Namespace != "" && target.Name != "" && target.Uid != ""
}

func validServiceAccount(account *ServiceAccountIdentity) bool {
	return account != nil && account.GetAccount() != nil && account.GetAccount().GetNamespace() != "" && account.GetAccount().GetName() != "" && account.GetUid() != ""
}

func validIdentity(identity *identitypb.Identity) bool {
	return identity != nil && identity.GetName() != "" && identity.GetUid() != ""
}

func validWakeCaller(caller WakeCaller) bool {
	switch caller := caller.(type) {
	case AccountWakeCaller:
		return validServiceAccount(caller.Account)
	case IdentityWakeCaller:
		return caller.Identity.GetName() != "" && caller.Identity.GetUid() != ""
	case ServiceWakeCaller:
		return auth.ValidateServiceCallers([]string{caller.SPIFFEID}) == nil
	default:
		return false
	}
}

func allowed(reason environmentlifecyclepb.ResumeEligibilityReason) *environmentlifecyclepb.ResumeEligibility {
	return &environmentlifecyclepb.ResumeEligibility{State: environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE, Reason: reason}
}

func denied(reason environmentlifecyclepb.ResumeEligibilityReason) *environmentlifecyclepb.ResumeEligibility {
	return &environmentlifecyclepb.ResumeEligibility{State: environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED, Reason: reason}
}

func wakeReasonForEligibility(eligibility *environmentlifecyclepb.ResumeEligibility) environmentlifecyclepb.WakeReason {
	switch eligibility.GetReason() {
	case environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP:
		return environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP
	case environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED:
		return environmentlifecyclepb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED
	case environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED:
		return environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED
	default:
		return environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED
	}
}

func assessment(state environmentlifecyclepb.WakeState, reason environmentlifecyclepb.WakeReason, generation *int64) *environmentlifecyclepb.WakeAssessment {
	result := &environmentlifecyclepb.WakeAssessment{State: state, Reason: reason, ObservedAt: timestamppb.New(time.Now().UTC())}
	if generation != nil && *generation >= 0 {
		result.TargetGeneration = proto.Int64(*generation)
	}
	return result
}

// AssessmentFor records a typed lifecycle outcome with its observed generation and timestamp.
func AssessmentFor(state environmentlifecyclepb.WakeState, reason environmentlifecyclepb.WakeReason, generation int64) *environmentlifecyclepb.WakeAssessment {
	return assessment(state, reason, &generation)
}

func assessmentFromError(err error) *environmentlifecyclepb.WakeAssessment {
	if status.Code(err) == codes.NotFound {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil)
	}
	return failureAssessment(codes.Unavailable, "Sandbox state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, -1)
}

func failureAssessment(code codes.Code, message string, reason environmentlifecyclepb.WakeReason, generation int64) *environmentlifecyclepb.WakeAssessment {
	result := AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, reason, generation)
	result.Error = status.New(code, message).Proto()
	return result
}

func unknownAssessment(generation int64) *environmentlifecyclepb.WakeAssessment {
	result := AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN, generation)
	result.Error = status.Convert(status.Error(codes.Unavailable, "guarded Sandbox patch outcome is unknown")).Proto()
	return result
}

func rateLimited(delay time.Duration, generation int64) *environmentlifecyclepb.WakeAssessment {
	result := AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED, environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED, generation)
	if delay > 0 {
		result.RetryAt = timestamppb.New(time.Now().Add(delay).UTC())
	}
	return result
}

func permissionDenied(eligibility *environmentlifecyclepb.ResumeEligibility) error {
	result := status.New(codes.PermissionDenied, "caller is not authorized to resume this Sandbox")
	withDetails, err := result.WithDetails(eligibility)
	if err != nil {
		return result.Err()
	}
	return withDetails.Err()
}

// ApplyExplicitResume reauthorizes an admitted caller on every guarded write attempt. The caller
// must capture the token before this operation, including tokenless operator requests.
func (s *Policy) ApplyExplicitResume(ctx context.Context, target *Target, caller WakeCaller, token string, operator bool) *environmentlifecyclepb.WakeAssessment {
	return s.applyResume(ctx, target, caller, token, operator, false, nil)
}

// ApplyAutomaticWake applies automatic policy and invokes the Dispatcher reservation immediately
// before each optimistic patch. A reservation is required; unknown patch outcomes are not replayed.
func (s *Policy) ApplyAutomaticWake(ctx context.Context, target *Target, caller WakeCaller, reserve func(context.Context, int64) (time.Duration, error)) (*environmentlifecyclepb.WakeAssessment, error) {
	if !ValidTarget(target) {
		return nil, errInvalidTarget
	}
	if !validWakeCaller(caller) {
		return nil, errInvalidCaller
	}
	if reserve == nil {
		return nil, errors.New("automatic wake reservation is required")
	}
	if target.Namespace != s.namespace {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	return s.applyResume(ctx, target, caller, "", false, true, reserve), nil
}
