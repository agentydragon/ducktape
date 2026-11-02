package lifecycle

import (
	"context"
	"errors"
	"math"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const maxPatchAttempts = 5

var (
	errInvalidTarget = errors.New("exact Sandbox target is required")
	errInvalidCaller = errors.New("effective ServiceAccount identity is incomplete")
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

// Service owns permission checks and optimistic Sandbox desired-state changes for explicit and
// automatic resume. Objects must be an uncached API client so every authorization and patch uses
// current target, policy, caller-account and execution-account facts.
type Service struct {
	lifecyclepb.UnimplementedEnvironmentLifecycleServer

	namespace       string
	objects         client.Client
	sandboxes       *sandboxreader.Reader
	wakeBudget      WakeBudget
	wakeCallers     map[auth.Account]struct{}
	operatorCallers map[auth.Account]struct{}
}

func NewService(options Options) (*Service, error) {
	if options.Namespace == "" || options.Objects == nil || options.WakeBudget == nil {
		return nil, errors.New("Sandbox namespace, uncached Kubernetes client, and shared wake budget are required")
	}
	sandboxes, err := sandboxreader.New(options.Namespace, options.Objects)
	if err != nil {
		return nil, err
	}
	return &Service{
		namespace: options.Namespace, objects: options.Objects, sandboxes: sandboxes, wakeBudget: options.WakeBudget,
		wakeCallers: accountSet(options.WakeCallers), operatorCallers: accountSet(options.OperatorCallers),
	}, nil
}

func accountSet(accounts []auth.Account) map[auth.Account]struct{} {
	result := make(map[auth.Account]struct{}, len(accounts))
	for _, account := range accounts {
		result[account] = struct{}{}
	}
	return result
}

// Methods admits reviewed workload ServiceAccounts. The composition root may add the shared
// delegated mode to ResumeEnvironment; the handler still evaluates only EffectiveCaller for
// ordinary permission and never promotes the delegated actor to operator authority.
func Methods(resolver *auth.Resolver) auth.Methods {
	return auth.Methods{
		lifecyclepb.EnvironmentLifecycle_ResumeEnvironment_FullMethodName: auth.ReviewedServiceAccount(resolver),
	}
}

// Eligibility reports the current explicit-resume authority and the generation from the same
// uncached Sandbox read. Trusted operator eligibility is reported only for direct Pod-bound calls.
func (s *Service) Eligibility(ctx context.Context, target Target) (*environmentpb.ResumeEligibility, int64, error) {
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
func (s *Service) RequestWake(ctx context.Context, target Target, caller ServiceAccount) (*environmentpb.WakeAssessment, error) {
	if !validTarget(target) {
		return nil, errInvalidTarget
	}
	if !validServiceAccount(caller) {
		return nil, errInvalidCaller
	}
	return s.requestWake(ctx, target, caller)
}

// AssessAutomaticWake reports current automatic-wake policy without reserving budget or changing
// Sandbox state. Discovery uses it to describe whether a suspended target may wake on delivery.
func (s *Service) AssessAutomaticWake(ctx context.Context, target Target, caller ServiceAccount) (*environmentpb.WakeAssessment, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
		return assessment(environmentpb.WakeState_WAKE_STATE_FAILED, environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	if !validServiceAccount(caller) {
		return nil, errInvalidCaller
	}
	view, err := s.readTarget(ctx, target)
	if err != nil {
		return assessmentFromError(err), nil
	}
	result, _ := s.automaticWakeAssessment(ctx, view, caller)
	return result, nil
}

// ResumeEnvironment is the public explicit-resume RPC. REQUESTED means the guarded desired-state
// write was accepted; it does not mean the Sandbox is Ready.
func (s *Service) ResumeEnvironment(
	ctx context.Context,
	request *lifecyclepb.ResumeEnvironmentRequest,
) (*lifecyclepb.ResumeEnvironmentResponse, error) {
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
	if eligibility.GetState() != environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		return nil, permissionDenied(eligibility)
	}
	if request.GetResumeToken() == "" && !operator {
		return nil, status.Error(codes.InvalidArgument, "resume_token is required")
	}
	if token := request.GetResumeToken(); token != "" && token != NewResumeToken(target, view.Spec.WakeGeneration) {
		return stateChangedResponse(target, view, eligibility, environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED), nil
	}

	resumeToken := request.GetResumeToken()
	if resumeToken == "" {
		// A direct operator may start a fresh intent without a Discovery token. Capture the
		// observed generation anyway so conflict retries cannot silently advance a later one.
		resumeToken = NewResumeToken(target, view.Spec.WakeGeneration)
	}
	assessment, err := s.applyResume(ctx, target, caller, resumeToken, operator, false)
	if err != nil {
		return nil, err
	}
	switch assessment.GetState() {
	case environmentpb.WakeState_WAKE_STATE_REQUESTED:
		return resumeResponse(target, assessment.GetTargetGeneration(), lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED, eligibility, assessment), nil
	case environmentpb.WakeState_WAKE_STATE_NOT_NEEDED:
		return resumeResponse(target, assessment.GetTargetGeneration(), lifecyclepb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING, eligibility, assessment), nil
	case environmentpb.WakeState_WAKE_STATE_RATE_LIMITED:
		return nil, rpcWithAssessment(codes.ResourceExhausted, "resume wake budget exhausted", assessment)
	case environmentpb.WakeState_WAKE_STATE_DENIED:
		return nil, permissionDenied(eligibilityFromWakeReason(assessment.GetReason()))
	case environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED:
		if assessment.GetReason() != environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED {
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
	case environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return nil, rpcWithAssessment(codes.Unavailable, "resume outcome is unknown", assessment)
	case environmentpb.WakeState_WAKE_STATE_FAILED:
		return nil, assessmentRPCError(assessment)
	default:
		return nil, status.Error(codes.Unavailable, "resume state is unavailable")
	}
}

func (s *Service) requestWake(ctx context.Context, target Target, caller ServiceAccount) (*environmentpb.WakeAssessment, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
		return assessment(environmentpb.WakeState_WAKE_STATE_FAILED, environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil), nil
	}
	assessment, err := s.applyResume(ctx, target, caller, "", false, true)
	return assessment, err
}

func (s *Service) applyResume(
	ctx context.Context,
	target Target,
	caller ServiceAccount,
	resumeToken string,
	operator bool,
	automatic bool,
) (*environmentpb.WakeAssessment, error) {
	for attempt := 0; attempt < maxPatchAttempts; attempt++ {
		view, err := s.readTarget(ctx, target)
		if err != nil {
			return assessmentFromError(err), nil
		}
		var eligibility *environmentpb.ResumeEligibility
		if !automatic {
			eligibility, err = s.eligibilityFor(ctx, view, caller, operator)
			if err != nil {
				return failureAssessment(codes.Unavailable, "caller or execution account state is unavailable", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
			}
			if eligibility.GetState() != environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
				return assessmentFor(environmentpb.WakeState_WAKE_STATE_DENIED, wakeReasonForEligibility(eligibility), view.Spec.WakeGeneration), nil
			}
		}
		if automatic {
			wakePolicy, permitted := s.automaticWakeAssessment(ctx, view, caller)
			if !permitted {
				return wakePolicy, nil
			}
		}
		if resumeToken != "" && resumeToken != NewResumeToken(target, view.Spec.WakeGeneration) {
			return assessmentFor(environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED, environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED, view.Spec.WakeGeneration), nil
		}
		if view.Spec.WakeGeneration == math.MaxInt64 {
			return failureAssessment(codes.FailedPrecondition, "wake generation cannot be incremented", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
		}
		if view.Status.StopStage == sandboxapi.StopStageDeleting || view.DeletionTimestamp != nil {
			return failureAssessment(codes.NotFound, "Sandbox is deleting", environmentpb.WakeReason_WAKE_REASON_DELETING, view.Spec.WakeGeneration), nil
		}
		if view.Status.StopStage == sandboxapi.StopStageQuiescing {
			return failureAssessment(codes.FailedPrecondition, "Sandbox is quiescing", environmentpb.WakeReason_WAKE_REASON_QUIESCING, view.Spec.WakeGeneration), nil
		}

		idleSuspended := view.Spec.Running && view.Status.OperatingMode == sandboxapi.OperatingModeSuspended &&
			view.Status.StopStage == sandboxapi.StopStageStopped && view.Status.SuspendedBy != nil &&
			*view.Status.SuspendedBy == sandboxapi.SuspendedByIdle && view.Status.SuspendedAtWakeGeneration != nil &&
			*view.Status.SuspendedAtWakeGeneration == view.Spec.WakeGeneration
		if !idleSuspended {
			if view.Spec.Running {
				return assessmentFor(environmentpb.WakeState_WAKE_STATE_NOT_NEEDED, environmentpb.WakeReason_WAKE_REASON_UNSPECIFIED, view.Spec.WakeGeneration), nil
			}
			if !operator {
				return assessmentFor(environmentpb.WakeState_WAKE_STATE_DENIED, environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, view.Spec.WakeGeneration), nil
			}
		}

		nextGeneration := view.Spec.WakeGeneration + 1
		delay, err := s.wakeBudget.ReserveWakeAttempt(ctx, caller, target.UID, nextGeneration)
		if err != nil {
			return failureAssessment(codes.Unavailable, "shared wake budget is unavailable", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, view.Spec.WakeGeneration), nil
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
		reason := environmentpb.WakeReason_WAKE_REASON_UNSPECIFIED
		if automatic && s.isWakeBroker(caller) && (eligibility == nil || eligibility.GetState() != environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE) {
			reason = environmentpb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER
		}
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_REQUESTED, reason, nextGeneration), nil
	}
	return failureAssessment(codes.Aborted, "Sandbox changed during all guarded resume attempts", environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED, -1), nil
}

func (s *Service) automaticWakeAssessment(ctx context.Context, view *sandboxapi.Sandbox, caller ServiceAccount) (*environmentpb.WakeAssessment, bool) {
	generation := view.Spec.WakeGeneration
	if view.Status.StopStage == sandboxapi.StopStageDeleting || view.DeletionTimestamp != nil {
		return failureAssessment(codes.NotFound, "Sandbox is deleting", environmentpb.WakeReason_WAKE_REASON_DELETING, generation), false
	}
	if view.Status.StopStage == sandboxapi.StopStageQuiescing {
		return failureAssessment(codes.FailedPrecondition, "Sandbox is quiescing", environmentpb.WakeReason_WAKE_REASON_QUIESCING, generation), false
	}
	if requestedStop(view) {
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_DENIED, environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP, generation), false
	}
	if !view.Spec.WakeOnSend {
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_AUTOMATIC_WAKE_DISABLED, environmentpb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED, generation), false
	}
	live, err := s.callerAccountCurrent(ctx, caller)
	if err != nil {
		return failureAssessment(codes.Unavailable, "caller ServiceAccount state is unavailable", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, generation), false
	}
	if !live {
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_DENIED, environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED, generation), false
	}
	if s.isWakeBroker(caller) {
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_ELIGIBLE, environmentpb.WakeReason_WAKE_REASON_ALLOWED_WAKE_BROKER, generation), true
	}
	eligibility, err := s.eligibilityFor(ctx, view, caller, false)
	if err != nil {
		return failureAssessment(codes.Unavailable, "execution account state is unavailable", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, generation), false
	}
	if eligibility.GetState() != environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		return assessmentFor(environmentpb.WakeState_WAKE_STATE_DENIED, wakeReasonForEligibility(eligibility), generation), false
	}
	return assessmentFor(environmentpb.WakeState_WAKE_STATE_ELIGIBLE, environmentpb.WakeReason_WAKE_REASON_UNSPECIFIED, generation), true
}

func (s *Service) eligibilityFor(ctx context.Context, view *sandboxapi.Sandbox, caller ServiceAccount, operator bool) (*environmentpb.ResumeEligibility, error) {
	live, err := s.callerAccountCurrent(ctx, caller)
	if err != nil {
		return nil, status.Error(codes.Unavailable, "caller ServiceAccount state is unavailable")
	}
	if !live {
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED), nil
	}
	if operator {
		return allowed(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_TRUSTED_OPERATOR), nil
	}
	if requestedStop(view) {
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP), nil
	}
	account := auth.Account{Namespace: caller.Namespace, Name: caller.Name}
	if account.Namespace != view.Namespace || account.Name != view.Name {
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT), nil
	}
	execution, found, err := s.sandboxes.ExecutionAccount(ctx, sandboxreader.Identity{
		Namespace: view.Namespace, Name: view.Name, UID: string(view.UID),
	})
	if err != nil {
		return nil, status.Error(codes.Unavailable, "execution ServiceAccount state is unavailable")
	}
	if !found || execution.Namespace != caller.Namespace || execution.Name != caller.Name || execution.UID != caller.UID {
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED), nil
	}
	if view.Spec.ResumePolicy == nil || !view.Spec.ResumePolicy.AllowOwnServiceAccount {
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED), nil
	}
	return allowed(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_ALLOWED_OWN_SERVICE_ACCOUNT), nil
}

func requestedStop(view *sandboxapi.Sandbox) bool {
	return !view.Spec.Running || (view.Status.SuspendedBy != nil && *view.Status.SuspendedBy == sandboxapi.SuspendedByRequested)
}

func (s *Service) isWakeBroker(caller ServiceAccount) bool {
	_, trustedBroker := s.wakeCallers[auth.Account{Namespace: caller.Namespace, Name: caller.Name}]
	return trustedBroker
}

func (s *Service) directOperator(ctx context.Context, caller ServiceAccount) bool {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return false
	}
	direct, ok := admission.(auth.DirectAdmission)
	if !ok || direct.Pod == nil || !direct.Pod.IsPodBound() || !identityMatches(direct.Caller, caller) {
		return false
	}
	_, configured := s.operatorCallers[direct.Caller.Account]
	return configured
}

func (s *Service) callerAccountCurrent(ctx context.Context, caller ServiceAccount) (bool, error) {
	if !validServiceAccount(caller) {
		return false, nil
	}
	var account corev1.ServiceAccount
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: caller.Namespace, Name: caller.Name}, &account); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	return account.UID != "" && string(account.UID) == caller.UID && account.DeletionTimestamp == nil, nil
}

func (s *Service) readTarget(ctx context.Context, target Target) (*sandboxapi.Sandbox, error) {
	if !validTarget(target) || target.Namespace != s.namespace {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	}
	var object sandboxapi.Sandbox
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: target.Namespace, Name: target.Name}, &object); apierrors.IsNotFound(err) {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	} else if err != nil {
		return nil, status.Error(codes.Unavailable, "Sandbox state is unavailable")
	}
	if string(object.UID) != target.UID || object.DeletionTimestamp != nil {
		return nil, status.Error(codes.NotFound, "Sandbox target not found")
	}
	return &object, nil
}

func effectiveCaller(ctx context.Context) (ServiceAccount, error) {
	caller, err := auth.EffectiveCaller(ctx)
	if err != nil {
		return ServiceAccount{}, err
	}
	result := ServiceAccount{Namespace: caller.Account.Namespace, Name: caller.Account.Name, UID: caller.ServiceAccountUID}
	if !validServiceAccount(result) {
		return ServiceAccount{}, status.Error(codes.Unauthenticated, errInvalidCaller.Error())
	}
	return result, nil
}

func identityMatches(principal auth.ServiceAccountPrincipal, identity ServiceAccount) bool {
	return principal.Account.Namespace == identity.Namespace && principal.Account.Name == identity.Name &&
		principal.ServiceAccountUID == identity.UID
}

func validTarget(target Target) bool {
	return target.Namespace != "" && target.Name != "" && target.UID != ""
}

func validServiceAccount(account ServiceAccount) bool {
	return account.Namespace != "" && account.Name != "" && account.UID != ""
}

func targetFromProto(target *discoverypb.TargetHandle) Target {
	if target == nil {
		return Target{}
	}
	return Target{Namespace: target.GetNamespace(), Name: target.GetName(), UID: target.GetUid()}
}

func targetProto(target Target) *discoverypb.TargetHandle {
	return &discoverypb.TargetHandle{Namespace: target.Namespace, Name: target.Name, Uid: target.UID}
}

func allowed(reason environmentpb.ResumeEligibilityReason) *environmentpb.ResumeEligibility {
	return &environmentpb.ResumeEligibility{State: environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE, Reason: reason}
}

func denied(reason environmentpb.ResumeEligibilityReason) *environmentpb.ResumeEligibility {
	return &environmentpb.ResumeEligibility{State: environmentpb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED, Reason: reason}
}

func wakeReasonForEligibility(eligibility *environmentpb.ResumeEligibility) environmentpb.WakeReason {
	switch eligibility.GetReason() {
	case environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP:
		return environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP
	case environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED:
		return environmentpb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED
	case environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED:
		return environmentpb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED
	default:
		return environmentpb.WakeReason_WAKE_REASON_CALLER_NOT_AUTHORIZED
	}
}

func assessment(state environmentpb.WakeState, reason environmentpb.WakeReason, generation *int64) *environmentpb.WakeAssessment {
	result := &environmentpb.WakeAssessment{State: state, Reason: reason, ObservedAt: timestamppb.New(time.Now().UTC())}
	if generation != nil && *generation >= 0 {
		result.TargetGeneration = proto.Uint64(uint64(*generation))
	}
	return result
}

func assessmentFor(state environmentpb.WakeState, reason environmentpb.WakeReason, generation int64) *environmentpb.WakeAssessment {
	return assessment(state, reason, &generation)
}

func assessmentFromError(err error) *environmentpb.WakeAssessment {
	if status.Code(err) == codes.NotFound {
		return assessment(environmentpb.WakeState_WAKE_STATE_FAILED, environmentpb.WakeReason_WAKE_REASON_TARGET_CHANGED, nil)
	}
	return failureAssessment(codes.Unavailable, "Sandbox state is unavailable", environmentpb.WakeReason_WAKE_REASON_BACKEND_STATE_UNAVAILABLE, -1)
}

func eligibilityFromWakeReason(reason environmentpb.WakeReason) *environmentpb.ResumeEligibility {
	switch reason {
	case environmentpb.WakeReason_WAKE_REASON_OPERATOR_REQUESTED_STOP:
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_OPERATOR_REQUESTED_STOP)
	case environmentpb.WakeReason_WAKE_REASON_RESUME_POLICY_DISABLED:
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED)
	case environmentpb.WakeReason_WAKE_REASON_EXECUTION_ACCOUNT_CHANGED:
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_EXECUTION_ACCOUNT_CHANGED)
	default:
		return denied(environmentpb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_CALLER_NOT_EXECUTION_ACCOUNT)
	}
}

func failureAssessment(code codes.Code, message string, reason environmentpb.WakeReason, generation int64) *environmentpb.WakeAssessment {
	result := assessmentFor(environmentpb.WakeState_WAKE_STATE_FAILED, reason, generation)
	result.Error = status.New(code, message).Proto()
	return result
}

func unknownAssessment(generation int64) *environmentpb.WakeAssessment {
	result := assessmentFor(environmentpb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN, environmentpb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN, generation)
	result.Error = status.Convert(status.Error(codes.Unavailable, "guarded Sandbox patch outcome is unknown")).Proto()
	return result
}

func rateLimited(delay time.Duration, generation int64) *environmentpb.WakeAssessment {
	result := assessmentFor(environmentpb.WakeState_WAKE_STATE_RATE_LIMITED, environmentpb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED, generation)
	if delay > 0 {
		result.RetryAt = timestamppb.New(time.Now().Add(delay).UTC())
	}
	return result
}

func permissionDenied(eligibility *environmentpb.ResumeEligibility) error {
	result := status.New(codes.PermissionDenied, "caller is not authorized to resume this Sandbox")
	withDetails, err := result.WithDetails(eligibility)
	if err != nil {
		return result.Err()
	}
	return withDetails.Err()
}

func rpcWithAssessment(code codes.Code, message string, assessment *environmentpb.WakeAssessment) error {
	result := status.New(code, message)
	withDetails, err := result.WithDetails(assessment)
	if err != nil {
		return result.Err()
	}
	return withDetails.Err()
}

func assessmentRPCError(assessment *environmentpb.WakeAssessment) error {
	code := codes.Unavailable
	message := "Sandbox state is unavailable"
	if assessment.GetError() != nil {
		code = codes.Code(assessment.GetError().GetCode())
		message = assessment.GetError().GetMessage()
	}
	return rpcWithAssessment(code, message, assessment)
}

func resumeResponse(
	target Target,
	generation uint64,
	outcome lifecyclepb.ResumeOutcome,
	eligibility *environmentpb.ResumeEligibility,
	wake *environmentpb.WakeAssessment,
) *lifecyclepb.ResumeEnvironmentResponse {
	return &lifecyclepb.ResumeEnvironmentResponse{
		Target: targetProto(target), Outcome: outcome, WakeGeneration: proto.Uint64(generation), Eligibility: eligibility, Wake: wake,
	}
}

func stateChangedResponse(
	target Target,
	view *sandboxapi.Sandbox,
	eligibility *environmentpb.ResumeEligibility,
	reason environmentpb.WakeReason,
) *lifecyclepb.ResumeEnvironmentResponse {
	wake := assessmentFor(environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED, reason, view.Spec.WakeGeneration)
	return resumeResponse(target, uint64(view.Spec.WakeGeneration), lifecyclepb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED, eligibility, wake)
}

var _ lifecyclepb.EnvironmentLifecycleServer = (*Service)(nil)
