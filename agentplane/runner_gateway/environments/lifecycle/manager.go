package lifecycle

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth/auth"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
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
	errInvalidCaller = errors.New("direct ServiceAccount identity is incomplete")
)

// Options configure the shared lifecycle policy and mutation owner. WakeCallers preserve the
// existing, restricted automatic-wake broker grant; they do not authorize explicit RPC calls or
// override a requested stop. OperatorCallers are a separate explicit-resume grant and are only
// honored for direct, Pod-bound admissions.
type Options struct {
	Namespace       string
	Objects         client.Client
	WakeBudget      WakeBudget
	WakeCallers     []auth.Account
	OperatorCallers []auth.Account
}

// Manager owns permission checks and optimistic Sandbox desired-state changes for explicit and
// automatic resume. Objects must be an uncached API client so every authorization and patch uses
// current target, policy, caller-account and execution-account facts.
type Manager struct {
	namespace       string
	objects         client.Client
	sandboxes       *sandboxreader.Reader
	wakeBudget      WakeBudget
	wakeCallers     sets.Set[auth.Account]
	operatorCallers sets.Set[auth.Account]
}

func NewManager(options Options) (*Manager, error) {
	if options.Namespace == "" || options.Objects == nil || options.WakeBudget == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes client, and shared wake budget are required")
	}
	sandboxes, err := sandboxreader.New(options.Namespace, options.Objects)
	if err != nil {
		return nil, err
	}
	return &Manager{
		namespace: options.Namespace, objects: options.Objects, sandboxes: sandboxes, wakeBudget: options.WakeBudget,
		wakeCallers: sets.New(options.WakeCallers...), operatorCallers: sets.New(options.OperatorCallers...),
	}, nil
}

// Eligibility reports the current explicit-resume authority and the generation from the same
// uncached Sandbox read. Trusted operator eligibility is reported only for direct Pod-bound calls.
func (s *Manager) Eligibility(ctx context.Context, target *Target) (*environmentlifecyclepb.ResumeEligibility, int64, error) {
	caller, err := effectiveCaller(ctx)
	if err != nil {
		return nil, 0, err
	}
	view, err := s.readTarget(ctx, target)
	if err != nil {
		return nil, 0, err
	}
	eligibility, err := s.eligibilityFor(ctx, view, caller, s.directOperator(ctx, caller))
	if err != nil {
		return nil, 0, err
	}
	return eligibility, view.Spec.WakeGeneration, nil
}

// RequestWake evaluates automatic-wake authority for an already admitted effective caller and,
// when allowed, performs one guarded desired-state mutation. Expected policy and operational
// outcomes are returned as WakeAssessment values; Go errors indicate malformed invocation.
func (s *Manager) RequestWake(ctx context.Context, target *Target, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	if !validTarget(target) {
		return nil, errInvalidTarget
	}
	if !validWakeCaller(caller) {
		return nil, errInvalidCaller
	}
	return s.requestWake(ctx, target, caller)
}

// AssessAutomaticWake reports current automatic-wake policy without reserving budget or changing
// Sandbox state. Discovery uses it to describe whether a suspended target may wake on delivery.
func (s *Manager) AssessAutomaticWake(ctx context.Context, target *Target, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	if !validWakeCaller(caller) {
		return nil, errInvalidCaller
	}
	view, err := s.readTarget(ctx, target)
	if err != nil {
		return assessmentFromError(err), nil
	}
	result, _ := s.automaticWakeAssessment(ctx, view, caller)
	return result, nil
}

// ResumeEnvironment handles EnvironmentService's explicit-resume request. REQUESTED means the
// guarded desired-state write was accepted; it does not mean the Sandbox is Ready.
func (s *Manager) ResumeEnvironment(
	ctx context.Context,
	request *environmentpb.ResumeEnvironmentRequest,
) (*environmentpb.ResumeEnvironmentResponse, error) {
	if request == nil || request.Target == nil {
		return nil, status.Error(codes.InvalidArgument, "target is required")
	}
	target := targetFromProto(request.Target)
	if !validTarget(target) {
		return nil, status.Error(codes.InvalidArgument, "target namespace, name and UID are required")
	}
	caller, err := effectiveCaller(ctx)
	if err != nil {
		return nil, err
	}
	view, err := s.readTarget(ctx, target)
	if err != nil {
		return nil, err
	}
	operator := s.directOperator(ctx, caller)
	eligibility, err := s.eligibilityFor(ctx, view, caller, operator)
	if err != nil {
		return nil, err
	}
	if eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		return nil, permissionDenied(eligibility)
	}
	if request.GetResumeToken() == "" && !operator {
		return nil, status.Error(codes.InvalidArgument, "resume_token is required")
	}
	if token := request.GetResumeToken(); token != "" && token != NewResumeToken(target, view.Spec.WakeGeneration) {
		return stateChangedResponse(target, view, eligibility, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED), nil
	}

	resumeToken := request.GetResumeToken()
	if resumeToken == "" {
		// A direct operator may start a fresh intent without a Discovery token. Capture the
		// observed generation anyway so conflict retries cannot silently advance a later one.
		resumeToken = NewResumeToken(target, view.Spec.WakeGeneration)
	}
	assessment, err := s.applyResume(ctx, target, AccountWakeCaller{Account: caller}, resumeToken, operator, false)
	if err != nil {
		return nil, err
	}
	switch assessment.GetState() {
	case environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED:
		return resumeResponse(target, assessment.GetTargetGeneration(), environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED, eligibility, assessment), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_NOT_NEEDED:
		return resumeResponse(target, assessment.GetTargetGeneration(), environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING, eligibility, assessment), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED:
		return nil, rpcWithAssessment(codes.ResourceExhausted, "resume wake budget exhausted", assessment)
	case environmentlifecyclepb.WakeState_WAKE_STATE_DENIED:
		return nil, permissionDenied(eligibilityFromWakeReason(assessment.GetReason()))
	case environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED:
		if assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED {
			return nil, status.Error(codes.Unavailable, "resume state is unavailable")
		}
		current, err := s.readTarget(ctx, target)
		if err != nil {
			return nil, err
		}
		currentEligibility, err := s.eligibilityFor(ctx, current, caller, operator)
		if err != nil {
			return nil, err
		}
		return stateChangedResponse(target, current, currentEligibility, assessment.GetReason()), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return nil, rpcWithAssessment(codes.Unavailable, "resume outcome is unknown", assessment)
	case environmentlifecyclepb.WakeState_WAKE_STATE_FAILED:
		return nil, assessmentRPCError(assessment)
	default:
		return nil, status.Error(codes.Unavailable, "resume state is unavailable")
	}
}

func (s *Manager) requestWake(ctx context.Context, target *Target, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	assessment, err := s.applyResume(ctx, target, caller, "", false, true)
	return assessment, err
}

func (s *Manager) applyResume(
	ctx context.Context,
	target *Target,
	caller WakeCaller,
	resumeToken string,
	operator bool,
	automatic bool,
) (*environmentlifecyclepb.WakeAssessment, error) {
	for attempt := 0; attempt < maxPatchAttempts; attempt++ {
		view, err := s.readTarget(ctx, target)
		if err != nil {
			return assessmentFromError(err), nil
		}
		var eligibility *environmentlifecyclepb.ResumeEligibility
		if !automatic {
			// Explicit resume is EnvironmentService's, which admits no Identity caller.
			account, ok := caller.(AccountWakeCaller)
			if !ok {
				return nil, errInvalidCaller
			}
			eligibility, err = s.eligibilityFor(ctx, view, account.Account, operator)
			if err != nil {
				return failureAssessment(codes.Unavailable, "caller or execution account state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
			}
			if eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
				return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, wakeReasonForEligibility(eligibility), view.Spec.WakeGeneration), nil
			}
		}
		if automatic {
			wakePolicy, permitted := s.automaticWakeAssessment(ctx, view, caller)
			if !permitted {
				return wakePolicy, nil
			}
		}
		if resumeToken != "" && resumeToken != NewResumeToken(target, view.Spec.WakeGeneration) {
			return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, view.Spec.WakeGeneration), nil
		}
		if view.Spec.WakeGeneration == math.MaxInt64 {
			return failureAssessment(codes.FailedPrecondition, "wake generation cannot be incremented", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
		}
		if stopping := stoppingAssessment(view); stopping != nil {
			return stopping, nil
		}

		idleSuspended := view.Spec.Running && view.Status.OperatingMode == sandboxapi.OperatingModeSuspended &&
			view.Status.StopStage == sandboxapi.StopStageStopped && view.Status.SuspendedBy != nil &&
			*view.Status.SuspendedBy == sandboxapi.SuspendedByIdle && view.Status.SuspendedAtWakeGeneration != nil &&
			*view.Status.SuspendedAtWakeGeneration == view.Spec.WakeGeneration
		if !idleSuspended {
			if view.Spec.Running {
				return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_NEEDED, environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED, view.Spec.WakeGeneration), nil
			}
			if !operator {
				return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, view.Spec.WakeGeneration), nil
			}
		}

		nextGeneration := view.Spec.WakeGeneration + 1
		delay, err := s.wakeBudget.ReserveWakeAttempt(ctx, s.budgetKey(caller), target.Uid, nextGeneration)
		if err != nil {
			return failureAssessment(codes.Unavailable, "shared wake budget is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
		}
		if delay > 0 {
			return rateLimited(delay, view.Spec.WakeGeneration), nil
		}
		updated := view.DeepCopy()
		updated.Spec.Running = true
		updated.Spec.WakeGeneration = nextGeneration
		err = s.objects.Patch(ctx, updated, client.MergeFromWithOptions(view, client.MergeFromWithOptimisticLock{}))
		if apierrors.IsConflict(err) {
			continue
		}
		if err != nil {
			return unknownAssessment(view.Spec.WakeGeneration), nil
		}
		reason := environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED
		if automatic && s.isWakeBroker(caller) && (eligibility == nil || eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE) {
			reason = environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER
		}
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED, reason, nextGeneration), nil
	}
	return failureAssessment(codes.Aborted, "Sandbox changed during all guarded resume attempts", environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, -1), nil
}

func (s *Manager) automaticWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, bool) {
	generation := view.Spec.WakeGeneration
	if stopping := stoppingAssessment(view); stopping != nil {
		return stopping, false
	}
	if requestedStop(view) {
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, generation), false
	}
	if !view.Spec.WakeOnSend {
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED, environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED, generation), false
	}
	switch caller := caller.(type) {
	case AccountWakeCaller:
		return s.accountWakeAssessment(ctx, view, caller.Account)
	case IdentityWakeCaller:
		return s.identityWakeAssessment(ctx, view, caller.Identity)
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
	return nil
}

func (s *Manager) accountWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller *ServiceAccountIdentity) (*environmentlifecyclepb.WakeAssessment, bool) {
	generation := view.Spec.WakeGeneration
	live, err := s.callerAccountCurrent(ctx, caller)
	if err != nil {
		return failureAssessment(codes.Unavailable, "caller ServiceAccount state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, generation), false
	}
	if !live {
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
	}
	if s.isWakeBroker(AccountWakeCaller{Account: caller}) {
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE, environmentlifecyclepb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER, generation), true
	}
	return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
}

// identityWakeAssessment authorizes the Sandbox's currently bound Identity under the existing
// resume-policy opt-in. It never resolves or falls back to the Sandbox's same-name ServiceAccount.
func (s *Manager) identityWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller *identitypb.Identity) (*environmentlifecyclepb.WakeAssessment, bool) {
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
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
	case named.GetUid() != caller.GetUid():
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED, generation), false
	case view.Spec.ResumePolicy == nil || !view.Spec.ResumePolicy.AllowOwnServiceAccount:
		return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_DENIED, environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED, generation), false
	}
	return assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_ELIGIBLE, environmentlifecyclepb.WakeReason_WAKE_REASON_UNSPECIFIED, generation), true
}

// budgetKey is whose wake budget caller spends. An Identity is keyed in the namespace the manager
// reads Identities in; its UID keeps it apart from a same-named ServiceAccount.
func (s *Manager) budgetKey(caller WakeCaller) WakeBudgetKey {
	switch caller := caller.(type) {
	case AccountWakeCaller:
		account := caller.Account.GetAccount()
		return WakeBudgetKey{Namespace: account.GetNamespace(), Name: account.GetName(), UID: caller.Account.GetUid()}
	case IdentityWakeCaller:
		return WakeBudgetKey{Namespace: s.namespace, Name: caller.Identity.GetName(), UID: caller.Identity.GetUid()}
	default:
		panic(fmt.Sprintf("unknown wake caller %T", caller))
	}
}

func (s *Manager) eligibilityFor(ctx context.Context, view *sandboxapi.Sandbox, caller *ServiceAccountIdentity, operator bool) (*environmentlifecyclepb.ResumeEligibility, error) {
	live, err := s.callerAccountCurrent(ctx, caller)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "caller ServiceAccount state is unavailable")
	}
	if !live {
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED), nil
	}
	if operator {
		return allowed(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_TRUSTED_OPERATOR), nil
	}
	if requestedStop(view) {
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP), nil
	}
	return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
}

func requestedStop(view *sandboxapi.Sandbox) bool {
	return !view.Spec.Running || (view.Status.SuspendedBy != nil && *view.Status.SuspendedBy == sandboxapi.SuspendedByRequested)
}

// isWakeBroker reports whether caller is a configured automatic-wake broker, which only a
// ServiceAccount can be.
func (s *Manager) isWakeBroker(caller WakeCaller) bool {
	accountCaller, ok := caller.(AccountWakeCaller)
	if !ok {
		return false
	}
	account := accountCaller.Account.GetAccount()
	if account == nil {
		return false
	}
	return s.wakeCallers.Has(auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()})
}

func (s *Manager) directOperator(ctx context.Context, caller *ServiceAccountIdentity) bool {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false
	}
	direct, ok := admission.(auth.DirectAdmission)
	if !ok || direct.Pod == nil || !direct.Pod.IsPodBound() || !identityMatches(direct.Caller, caller) {
		return false
	}
	account := direct.Caller.GetAccount()
	configured := s.operatorCallers.Has(auth.Account{Namespace: account.GetNamespace(), Name: account.GetName()})
	return configured
}

func (s *Manager) callerAccountCurrent(ctx context.Context, caller *ServiceAccountIdentity) (bool, error) {
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

func (s *Manager) readTarget(ctx context.Context, target *Target) (*sandboxapi.Sandbox, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
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

func effectiveCaller(ctx context.Context) (*ServiceAccountIdentity, error) {
	caller, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return nil, err
	}
	if !validServiceAccount(caller) {
		return nil, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
	}
	return caller, nil
}

func identityMatches(principal *auth.ServiceAccountPrincipal, identity *ServiceAccountIdentity) bool {
	account := identity.GetAccount()
	principalAccount := principal.GetAccount()
	return account != nil && principalAccount != nil && principalAccount.GetNamespace() == account.GetNamespace() &&
		principalAccount.GetName() == account.GetName() && principal.GetUid() == identity.GetUid()
}

func validTarget(target *Target) bool {
	return target != nil && target.Namespace != "" && target.Name != "" && target.Uid != ""
}

func validServiceAccount(account *ServiceAccountIdentity) bool {
	return account != nil && account.GetAccount() != nil && account.GetAccount().GetNamespace() != "" && account.GetAccount().GetName() != "" && account.GetUid() != ""
}

func validWakeCaller(caller WakeCaller) bool {
	switch caller := caller.(type) {
	case AccountWakeCaller:
		return validServiceAccount(caller.Account)
	case IdentityWakeCaller:
		return caller.Identity.GetName() != "" && caller.Identity.GetUid() != ""
	default:
		return false
	}
}

func targetFromProto(target *identitypb.EnvironmentTarget) *Target {
	if target == nil {
		return nil
	}
	return target
}

func targetProto(target *Target) *identitypb.EnvironmentTarget {
	return target
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

func assessmentFor(state environmentlifecyclepb.WakeState, reason environmentlifecyclepb.WakeReason, generation int64) *environmentlifecyclepb.WakeAssessment {
	return assessment(state, reason, &generation)
}

func assessmentFromError(err error) *environmentlifecyclepb.WakeAssessment {
	if status.Code(err) == codes.NotFound {
		return assessment(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil)
	}
	return failureAssessment(codes.Unavailable, "Sandbox state is unavailable", environmentlifecyclepb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, -1)
}

func eligibilityFromWakeReason(reason environmentlifecyclepb.WakeReason) *environmentlifecyclepb.ResumeEligibility {
	switch reason {
	case environmentlifecyclepb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP:
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP)
	case environmentlifecyclepb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED:
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED)
	case environmentlifecyclepb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED:
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED)
	default:
		return denied(environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT)
	}
}

func failureAssessment(code codes.Code, message string, reason environmentlifecyclepb.WakeReason, generation int64) *environmentlifecyclepb.WakeAssessment {
	result := assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_FAILED, reason, generation)
	result.Error = status.New(code, message).Proto()
	return result
}

func unknownAssessment(generation int64) *environmentlifecyclepb.WakeAssessment {
	result := assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN, generation)
	result.Error = status.Convert(status.Error(codes.Unavailable, "guarded Sandbox patch outcome is unknown")).Proto()
	return result
}

func rateLimited(delay time.Duration, generation int64) *environmentlifecyclepb.WakeAssessment {
	result := assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED, environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED, generation)
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

func rpcWithAssessment(code codes.Code, message string, assessment *environmentlifecyclepb.WakeAssessment) error {
	result := status.New(code, message)
	withDetails, err := result.WithDetails(assessment)
	if err != nil {
		return result.Err()
	}
	return withDetails.Err()
}

func assessmentRPCError(assessment *environmentlifecyclepb.WakeAssessment) error {
	code := codes.Unavailable
	message := "Sandbox state is unavailable"
	if assessment.GetError() != nil {
		code = codes.Code(assessment.GetError().GetCode())
		message = assessment.GetError().GetMessage()
	}
	return rpcWithAssessment(code, message, assessment)
}

func resumeResponse(
	target *Target,
	generation int64,
	outcome environmentpb.ResumeOutcome,
	eligibility *environmentlifecyclepb.ResumeEligibility,
	wake *environmentlifecyclepb.WakeAssessment,
) *environmentpb.ResumeEnvironmentResponse {
	return &environmentpb.ResumeEnvironmentResponse{
		Target: targetProto(target), Outcome: outcome, WakeGeneration: proto.Int64(generation), Eligibility: eligibility, Wake: wake,
	}
}

func stateChangedResponse(
	target *Target,
	view *sandboxapi.Sandbox,
	eligibility *environmentlifecyclepb.ResumeEligibility,
	reason environmentlifecyclepb.WakeReason,
) *environmentpb.ResumeEnvironmentResponse {
	wake := assessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED, reason, view.Spec.WakeGeneration)
	return resumeResponse(target, view.Spec.WakeGeneration, environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED, eligibility, wake)
}
