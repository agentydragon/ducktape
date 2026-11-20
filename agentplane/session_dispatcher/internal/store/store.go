// Package store stores accepted Session work before delivery. PostgreSQL is
// both the queue and the record of delivery; notifications are only invalidations.
package store

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/session_dispatcher/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/session_dispatcher/internal/placement"
	"github.com/lib/pq"
	"google.golang.org/protobuf/proto"
	"k8s.io/utils/ptr"
)

const (
	QueueState             = "QUEUED_IN_DISPATCHER"
	DeliveringState        = "DELIVERING_TO_RUNNER"
	AdmittedState          = "RUNNER_ADMITTED"
	FailedState            = "FAILED"
	CancelledState         = "CANCELLED_IN_DISPATCHER"
	UnknownState           = "UNKNOWN"
	SessionWaitingState    = "WAITING"
	SessionInProgressState = "IN_PROGRESS"
	SessionAcceptedState   = "ACCEPTED"
	SessionFailedState     = "FAILED"
	SessionUnknownState    = "UNKNOWN"
	WakeLimit              = 10
	WakeWindow             = time.Minute
	WakeDeadline           = 10 * time.Minute
	NotifyChannel          = "agentplane_dispatcher_dispatch"
)

var (
	ErrCommandConflict    = errors.New("command_id was used for other work")
	ErrSessionConflict    = errors.New("session_id was opened with another request")
	ErrQueueFull          = errors.New("session queue is full")
	ErrNotFound           = errors.New("dispatch not found")
	ErrNotCancellable     = errors.New("delivery already began")
	ErrNotAuthorized      = errors.New("caller does not own the Session")
	ErrSandboxDeleted     = errors.New("sandbox was deleted")
	ErrSessionUnconfirmed = errors.New("Session is not confirmed")
)

func serviceAccountIdentityFor(account auth.Account, uid string) *identitypb.ServiceAccountIdentity {
	return &identitypb.ServiceAccountIdentity{
		Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: uid,
	}
}

func accountOfIdentity(identity *identitypb.ServiceAccountIdentity) auth.Account {
	if identity == nil || identity.GetAccount() == nil {
		return auth.Account{}
	}
	return auth.Account{Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName()}
}

// SameIdentity compares Identity incarnations, nil meaning none.
func SameIdentity(left, right *identitypb.Identity) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.GetName() == right.GetName() && left.GetUid() == right.GetUid()
}

type PodIdentity struct {
	Name, UID string
}

type ActionCorrelation struct {
	RequestID, ExecutionID string
}

// AdmissionAttribution records the verified RPC actor separately from the effective caller.
// Action IDs are audit correlation only and never participate in authorization or idempotency.
type AdmissionAttribution struct {
	Actor           *identitypb.ServiceAccountIdentity
	ActorPod        *PodIdentity
	EffectiveCaller *identitypb.ServiceAccountIdentity
	Action          *ActionCorrelation
}

func (*AdmissionAttribution) isAttribution() {}

// IdentityAttribution records a caller whose workload SVID named an Identity, and the Pod its
// accepted incarnation runs in, nil for a standing workload.
type IdentityAttribution struct {
	Identity *identitypb.Identity
	Pod      *PodIdentity
}

func (*IdentityAttribution) isAttribution() {}

// DelegatedIdentityAttribution records a trusted platform component's call on behalf of an
// Identity: the Identity it was authorized as and the component that named it.
type DelegatedIdentityAttribution struct {
	Identity   *identitypb.Identity
	Delegation IdentityDelegation
}

func (*DelegatedIdentityAttribution) isAttribution() {}

// Attribution is the verified caller recorded with a row: an *AdmissionAttribution for a
// ServiceAccount caller, an *IdentityAttribution or a *DelegatedIdentityAttribution.
type Attribution interface{ isAttribution() }

// Caller is an admitted Dispatcher caller: a ServiceAccountCaller or an IdentityCaller.
type Caller interface{ isCaller() }

// ServiceAccountCaller distinguishes the caller's actual ServiceAccount from the explicit, Pod-bound
// backend path that retains the existing app/notification/Actions authority.
type ServiceAccountCaller struct {
	ServiceAccountIdentity *identitypb.ServiceAccountIdentity
	Privileged             bool
	Attribution            *AdmissionAttribution
}

// IdentityCaller is a call authorized as an Identity. It is never privileged: it owns the Sessions
// whose execution identity is its Identity incarnation, name and UID, however the Identity was
// proven.
type IdentityCaller struct {
	Identity *identitypb.Identity
	// Proof is an IdentitySVID or an IdentityDelegation.
	Proof IdentityProof
}

// IdentityProof is how a call proved its Identity.
type IdentityProof interface{ isIdentityProof() }

// IdentitySVID is a workload whose own SVID named the Identity, with the Pod its accepted
// incarnation runs in, nil for a standing workload.
type IdentitySVID struct {
	Pod *PodIdentity
}

// IdentityDelegation is a trusted platform component that named the Identity in a delegation
// envelope: its Pod-bound ServiceAccount and the Action the call served.
type IdentityDelegation struct {
	Actor    *identitypb.ServiceAccountIdentity
	ActorPod PodIdentity
	Action   ActionCorrelation
}

func (IdentitySVID) isIdentityProof()       {}
func (IdentityDelegation) isIdentityProof() {}

func (ServiceAccountCaller) isCaller() {}
func (IdentityCaller) isCaller()       {}

func Privileged(caller Caller) bool {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		return c.Privileged
	case ServiceCaller:
		return true // only exact configured component callers construct this type
	default:
		return false
	}
}

// callerAttribution is the Attribution a row records for caller, nil for an internal write.
func callerAttribution(caller Caller) Attribution {
	switch c := caller.(type) {
	case ServiceCaller:
		return &ServiceAttribution{ActorSPIFFEID: c.SPIFFEID}
	case ServiceAccountCaller:
		if c.Attribution == nil {
			return nil
		}
		return c.Attribution
	case IdentityCaller:
		switch proof := c.Proof.(type) {
		case IdentitySVID:
			return &IdentityAttribution{Identity: c.Identity, Pod: proof.Pod}
		case IdentityServiceDelegation:
			return &ServiceAttribution{ActorSPIFFEID: proof.ActorSPIFFEID, Identity: c.Identity, Action: &proof.Action}
		case IdentityDelegation:
			return &DelegatedIdentityAttribution{Identity: c.Identity, Delegation: proof}
		default:
			return nil
		}
	default:
		return nil
	}
}

// ValidateCaller checks the attribution caller will record.
func ValidateCaller(caller Caller, wakeCaller *auth.Account) error {
	if callerAttribution(caller) == nil {
		return errors.New("caller attribution is required")
	}
	switch c := caller.(type) {
	case ServiceCaller:
		if wakeCaller != nil {
			return errors.New("service caller records no Kubernetes wake account")
		}
		return validateServiceAttribution(&ServiceAttribution{ActorSPIFFEID: c.SPIFFEID})
	case ServiceAccountCaller:
		return ValidateAttribution(c.Attribution, wakeCaller)
	case IdentityCaller:
		if c.Identity.GetName() == "" || c.Identity.GetUid() == "" {
			return errors.New("Identity caller requires a name and UID")
		}
		switch proof := c.Proof.(type) {
		case IdentitySVID:
			if proof.Pod != nil && (proof.Pod.Name == "" || proof.Pod.UID == "") {
				return errors.New("Identity caller has incomplete Pod provenance")
			}
		case IdentityServiceDelegation:
			if err := validateServiceAttribution(&ServiceAttribution{ActorSPIFFEID: proof.ActorSPIFFEID, Identity: c.Identity, Action: &proof.Action}); err != nil {
				return err
			}
		case IdentityDelegation:
			if err := proof.validate(); err != nil {
				return err
			}
		default:
			return errors.New("Identity caller has no proof")
		}
		if wakeCaller != nil {
			return errors.New("an Identity caller records no wake caller account; its wake is assessed from its attribution")
		}
		return nil
	default:
		return fmt.Errorf("unknown Dispatcher caller %T", caller)
	}
}

// OwnerScope is the SQL condition limiting Session rows (columns qualified by prefix) to those caller
// may read, with its arguments numbered from next.
func OwnerScope(caller Caller, prefix string, next int) (string, []any) {
	switch c := caller.(type) {
	case ServiceCaller:
		return "TRUE", nil
	case ServiceAccountCaller:
		return fmt.Sprintf("$%d", next), []any{c.Privileged}
	case IdentityCaller:
		return fmt.Sprintf("(%[3]sexecution_identity_name=$%[1]d AND %[3]sexecution_identity_uid::text=$%[2]d)", next, next+1, prefix),
			[]any{c.Identity.GetName(), c.Identity.GetUid()}
	default:
		panic(fmt.Sprintf("unknown Dispatcher caller %T", caller))
	}
}

// identityAttributionValues are the identity attribution columns of a row attribution records.
func identityAttributionValues(attribution Attribution) []any {
	identity, ok := attribution.(*IdentityAttribution)
	if !ok {
		return []any{nil, nil, nil, nil}
	}
	var podName, podUID any
	if identity.Pod != nil {
		podName, podUID = identity.Pod.Name, identity.Pod.UID
	}
	return []any{identity.Identity.GetName(), identity.Identity.GetUid(), podName, podUID}
}

func sessionIdentityAttributionValues(attribution Attribution) []any {
	switch identity := attribution.(type) {
	case *IdentityAttribution:
		return identityAttributionValues(identity)
	case *DelegatedIdentityAttribution:
		return []any{identity.Identity.GetName(), identity.Identity.GetUid(), nil, nil}
	default:
		return []any{nil, nil, nil, nil}
	}
}

// sessionDelegationAttributionValues stores the verified delegating actor separately from the
// effective Identity that created the Session.
func sessionDelegationAttributionValues(attribution Attribution) []any {
	delegated, ok := attribution.(*DelegatedIdentityAttribution)
	if !ok {
		return []any{nil, nil, nil, nil, nil, nil, nil}
	}
	proof := delegated.Delegation
	return []any{
		proof.Actor.GetAccount().GetNamespace(), proof.Actor.GetAccount().GetName(), proof.Actor.GetUid(),
		proof.ActorPod.Name, proof.ActorPod.UID, proof.Action.RequestID, proof.Action.ExecutionID,
	}
}

func (d IdentityDelegation) validate() error {
	if d.Actor.GetAccount().GetNamespace() == "" || d.Actor.GetAccount().GetName() == "" || d.Actor.GetUid() == "" ||
		d.ActorPod.Name == "" || d.ActorPod.UID == "" || d.Action.RequestID == "" || d.Action.ExecutionID == "" {
		return errors.New("delegated Identity attribution is incomplete")
	}
	return nil
}

// delegatedIdentityAttributionValues are the delegated Identity columns of a row attribution records.
func delegatedIdentityAttributionValues(attribution Attribution) []any {
	delegated, ok := attribution.(*DelegatedIdentityAttribution)
	if !ok {
		return []any{nil, nil, nil, nil, nil, nil, nil, nil, nil}
	}
	proof := delegated.Delegation
	return []any{
		delegated.Identity.GetName(), delegated.Identity.GetUid(),
		proof.Actor.GetAccount().GetNamespace(), proof.Actor.GetAccount().GetName(), proof.Actor.GetUid(),
		proof.ActorPod.Name, proof.ActorPod.UID, proof.Action.RequestID, proof.Action.ExecutionID,
	}
}

// scanDelegatedIdentityAttribution reads a dispatch row's delegated Identity columns, nil when
// none is recorded.
func scanDelegatedIdentityAttribution(columns [9]sql.NullString) (*DelegatedIdentityAttribution, error) {
	recorded := 0
	for _, column := range columns {
		if column.Valid {
			recorded++
		}
	}
	switch recorded {
	case 0:
		return nil, nil
	case len(columns):
	default:
		return nil, errors.New("delegated Identity attribution is incomplete")
	}
	result := &DelegatedIdentityAttribution{
		Identity: &identitypb.Identity{Name: columns[0].String, Uid: columns[1].String},
		Delegation: IdentityDelegation{
			Actor:    serviceAccountIdentityFor(auth.Account{Namespace: columns[2].String, Name: columns[3].String}, columns[4].String),
			ActorPod: PodIdentity{Name: columns[5].String, UID: columns[6].String},
			Action:   ActionCorrelation{RequestID: columns[7].String, ExecutionID: columns[8].String},
		},
	}
	if err := result.Delegation.validate(); err != nil {
		return nil, err
	}
	return result, nil
}

func scanSessionCreatorDelegation(
	identityName, identityUID sql.NullString,
	columns [7]sql.NullString,
) (*DelegatedIdentityAttribution, error) {
	recorded := 0
	for _, column := range columns {
		if column.Valid {
			recorded++
		}
	}
	if recorded == 0 {
		return nil, nil
	}
	if recorded != len(columns) || !identityName.Valid || !identityUID.Valid {
		return nil, errors.New("delegated Identity creator attribution is incomplete")
	}
	result := &DelegatedIdentityAttribution{
		Identity: &identitypb.Identity{Name: identityName.String, Uid: identityUID.String},
		Delegation: IdentityDelegation{
			Actor:    serviceAccountIdentityFor(auth.Account{Namespace: columns[0].String, Name: columns[1].String}, columns[2].String),
			ActorPod: PodIdentity{Name: columns[3].String, UID: columns[4].String},
			Action:   ActionCorrelation{RequestID: columns[5].String, ExecutionID: columns[6].String},
		},
	}
	if err := result.Delegation.validate(); err != nil {
		return nil, err
	}
	return result, nil
}

// scanAttribution combines a row's ServiceAccount and Identity attribution columns.
func scanAttribution(account *AdmissionAttribution, identityName, identityUID, podName, podUID sql.NullString) (Attribution, error) {
	if identityName.Valid != identityUID.Valid || podName.Valid != podUID.Valid || podName.Valid && !identityName.Valid {
		return nil, errors.New("Identity attribution is incomplete")
	}
	if !identityName.Valid {
		if account == nil {
			return nil, nil
		}
		return account, nil
	}
	if account != nil {
		return nil, errors.New("row is attributed to both a ServiceAccount and an Identity")
	}
	result := &IdentityAttribution{Identity: &identitypb.Identity{Name: identityName.String, Uid: identityUID.String}}
	if podName.Valid {
		result.Pod = &PodIdentity{Name: podName.String, UID: podUID.String}
	}
	return result, nil
}

type Dispatch struct {
	DeliveryAttempted       bool
	Position                int64
	CommandID, SessionID    string
	SandboxName, SandboxUID string
	WakeEligible            bool
	WakeCaller              *auth.Account
	WakeFromIncarnation     *string
	WakeRequestedAt         *time.Time
	Operation               string
	Command                 *commandpb.Command
	State, Reason           string
	Admission               *eventlogpb.EventEntry
	DeliveryBlocker         *environmentpb.DeliveryBlocker
	WakeAssessment          *environmentpb.WakeAssessment
	Attribution             Attribution
	Traceparent             string
	CreatedAt, UpdatedAt    time.Time
}

type Session struct {
	ActivityLive                       bool
	Activity                           []byte
	Version                            int64
	SessionID, SandboxName, SandboxUID string
	Placement                          placement.SessionPlacement
	// ExecutionIdentity is the exact Identity incarnation the Session's Sandbox named at creation.
	// It is immutable.
	ExecutionIdentity *identitypb.Identity
	// Creator records the authenticated actor that brokered the session, separately from its owner
	// and effective caller. For a delegated session this is the Actions ServiceAccount.
	Creator              *identitypb.ServiceAccountIdentity
	Attribution          Attribution
	WakeEligible         bool
	WakeCaller           *auth.Account
	WakeFromIncarnation  *string
	WakeRequestedAt      *time.Time
	Spec                 *sessionpb.SessionSpec
	SetupScript          *string
	State, Reason        string
	DeliveryBlocker      *environmentpb.DeliveryBlocker
	WakeAssessment       *environmentpb.WakeAssessment
	Traceparent          string
	CreatedAt, UpdatedAt time.Time
	EnvironmentDeletedAt *time.Time
}

type Store struct {
	DB       *sql.DB
	QueueCap int
}

func ValidateAttribution(attribution *AdmissionAttribution, wakeCaller *auth.Account) error {
	if attribution == nil {
		return nil
	}
	for _, identity := range []*identitypb.ServiceAccountIdentity{attribution.Actor, attribution.EffectiveCaller} {
		if identity == nil || identity.GetAccount() == nil || identity.GetAccount().GetNamespace() == "" ||
			identity.GetAccount().GetName() == "" || identity.GetUid() == "" {
			return errors.New("admission attribution requires complete ServiceAccount identities")
		}
	}
	if wakeCaller != nil && *wakeCaller != accountOfIdentity(attribution.EffectiveCaller) {
		return errors.New("wake caller does not match admission effective caller")
	}
	if attribution.ActorPod != nil && (attribution.ActorPod.Name == "" || attribution.ActorPod.UID == "") {
		return errors.New("admission attribution has incomplete actor Pod proof")
	}
	if attribution.Action != nil && (attribution.Action.RequestID == "" || attribution.Action.ExecutionID == "") {
		return errors.New("admission attribution has incomplete Action correlation")
	}
	return nil
}

// attributionValues are the ServiceAccount attribution columns of a row attribution records.
func attributionValues(recorded Attribution) []any {
	attribution, ok := recorded.(*AdmissionAttribution)
	if !ok {
		return []any{nil, nil, nil, nil, nil, nil, nil, nil, nil, nil}
	}
	var actorPodName, actorPodUID, requestID, executionID any
	if attribution.ActorPod != nil {
		actorPodName, actorPodUID = attribution.ActorPod.Name, attribution.ActorPod.UID
	}
	if attribution.Action != nil {
		requestID, executionID = attribution.Action.RequestID, attribution.Action.ExecutionID
	}
	return []any{
		attribution.Actor.GetAccount().GetNamespace(), attribution.Actor.GetAccount().GetName(), attribution.Actor.GetUid(),
		actorPodName, actorPodUID,
		attribution.EffectiveCaller.GetAccount().GetNamespace(), attribution.EffectiveCaller.GetAccount().GetName(), attribution.EffectiveCaller.GetUid(),
		requestID, executionID,
	}
}

func scanIdentity(namespace, name, uid sql.NullString, field string) (*identitypb.ServiceAccountIdentity, error) {
	if namespace.Valid != name.Valid || namespace.Valid != uid.Valid {
		return nil, fmt.Errorf("%s attribution is incomplete", field)
	}
	if !namespace.Valid {
		return nil, nil
	}
	return serviceAccountIdentityFor(auth.Account{Namespace: namespace.String, Name: name.String}, uid.String), nil
}

func scanAdmissionAttribution(
	actor *identitypb.ServiceAccountIdentity,
	actorPodName, actorPodUID sql.NullString,
	effectiveCaller *identitypb.ServiceAccountIdentity,
	requestID, executionID sql.NullString,
	wakeCaller *auth.Account,
) (*AdmissionAttribution, error) {
	if actorPodName.Valid != actorPodUID.Valid || requestID.Valid != executionID.Valid {
		return nil, errors.New("admission attribution is incomplete")
	}
	hasAttribution := actorPodName.Valid || effectiveCaller != nil || requestID.Valid
	if !hasAttribution {
		return nil, nil
	}
	if actor == nil || effectiveCaller == nil {
		return nil, errors.New("admission attribution identities are incomplete")
	}
	attribution := &AdmissionAttribution{Actor: actor, EffectiveCaller: effectiveCaller}
	if actorPodName.Valid {
		attribution.ActorPod = &PodIdentity{Name: actorPodName.String, UID: actorPodUID.String}
	}
	if requestID.Valid {
		attribution.Action = &ActionCorrelation{RequestID: requestID.String, ExecutionID: executionID.String}
	}
	if err := ValidateAttribution(attribution, wakeCaller); err != nil {
		return nil, err
	}
	return attribution, nil
}

func NewStore(db *sql.DB, queueCap int) *Store { return &Store{DB: db, QueueCap: queueCap} }

func transaction(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, db, sql.TxOptions{}, f)
}

func notify(ctx context.Context, tx *sql.Tx, sessionID string) error {
	return postgres.Notify(ctx, tx, NotifyChannel, sessionID)
}

func sessionLock(ctx context.Context, tx *sql.Tx, sessionID string) error {
	_, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "session:"+sessionID)
	return err
}

// lockLiveSandbox holds admission open until commit. EndSandbox takes the exclusive
// lock before recording a durable tombstone, so stale inventory cannot resurrect work.
// Acquire this before Session, command and ownership locks.
func lockLiveSandbox(ctx context.Context, tx *sql.Tx, sandboxUID string) error {
	if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock_shared(hashtextextended($1, 0))", "sandbox:"+sandboxUID); err != nil {
		return err
	}
	var deleted bool
	if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM deleted_sandbox WHERE sandbox_uid=$1)", sandboxUID).Scan(&deleted); err != nil {
		return err
	}
	if deleted {
		return ErrSandboxDeleted
	}
	return nil
}

func operation(command *commandpb.Command) (string, error) {
	switch command.GetOperation().(type) {
	case *commandpb.Command_SubmitInput:
		return "submit_input", nil
	case *commandpb.Command_ChangeModel:
		return "change_model", nil
	case *commandpb.Command_ChangeReasoningEffort:
		return "change_reasoning_effort", nil
	case *commandpb.Command_InterruptTurn:
		return "interrupt_turn", nil
	default:
		return "", errors.New("not a stored command operation")
	}
}

func Wire(message proto.Message) ([]byte, error) {
	return proto.MarshalOptions{Deterministic: true}.Marshal(message)
}

func wireOptional(message proto.Message) ([]byte, error) {
	if message == nil {
		return nil, nil
	}
	return Wire(message)
}

func unmarshalDeliveryBlocker(data []byte) (*environmentpb.DeliveryBlocker, error) {
	if len(data) == 0 {
		return nil, nil
	}
	result := &environmentpb.DeliveryBlocker{}
	if err := proto.Unmarshal(data, result); err != nil {
		return nil, fmt.Errorf("decode delivery blocker: %w", err)
	}
	return result, nil
}

func unmarshalWakeAssessment(data []byte) (*environmentpb.WakeAssessment, error) {
	result := &environmentpb.WakeAssessment{}
	if len(data) == 0 {
		result.State = environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED
		return result, nil
	}
	if err := proto.Unmarshal(data, result); err != nil {
		return nil, fmt.Errorf("decode wake assessment: %w", err)
	}
	return result, nil
}

func initialWakeAssessment() *environmentpb.WakeAssessment {
	return &environmentpb.WakeAssessment{State: environmentpb.WakeState_WAKE_STATE_NOT_EVALUATED}
}

const dispatchColumns = `position, command_id, session_id, sandbox_name, sandbox_uid::text,
	wake_eligible, wake_caller_namespace, wake_caller_name, wake_from_incarnation,
	wake_requested_at, operation, command, state, reason, admission, traceparent,
	actor_account_namespace, actor_account_name, actor_account_uid, actor_pod_name, actor_pod_uid,
	effective_caller_account_namespace, effective_caller_account_name, effective_caller_account_uid,
	action_request_id::text, action_execution_id::text, created_at, updated_at,
	delivery_blocker, wake_assessment, delivery_attempted,
	actor_identity_name, actor_identity_uid, actor_identity_pod_name, actor_identity_pod_uid,
	delegated_identity_name, delegated_identity_uid, delegator_account_namespace, delegator_account_name,
	delegator_account_uid, delegator_pod_name, delegator_pod_uid,
	delegated_action_request_id::text, delegated_action_execution_id::text, service_attribution`

const qualifiedDispatchColumns = `d.position, d.command_id, d.session_id, d.sandbox_name, d.sandbox_uid::text,
	d.wake_eligible, d.wake_caller_namespace, d.wake_caller_name, d.wake_from_incarnation,
	d.wake_requested_at, d.operation, d.command, d.state, d.reason, d.admission, d.traceparent,
	d.actor_account_namespace, d.actor_account_name, d.actor_account_uid, d.actor_pod_name, d.actor_pod_uid,
	d.effective_caller_account_namespace, d.effective_caller_account_name, d.effective_caller_account_uid,
	d.action_request_id::text, d.action_execution_id::text, d.created_at, d.updated_at,
	d.delivery_blocker, d.wake_assessment, d.delivery_attempted,
	d.actor_identity_name, d.actor_identity_uid, d.actor_identity_pod_name, d.actor_identity_pod_uid,
	d.delegated_identity_name, d.delegated_identity_uid, d.delegator_account_namespace, d.delegator_account_name,
	d.delegator_account_uid, d.delegator_pod_name, d.delegator_pod_uid,
	d.delegated_action_request_id::text, d.delegated_action_execution_id::text, d.service_attribution`

func scanDispatch(row interface{ Scan(...any) error }) (*Dispatch, error) {
	d := &Dispatch{}
	var callerNS, callerName, incarnation, reason sql.NullString
	var actorNS, actorName, actorUID, actorPodName, actorPodUID sql.NullString
	var effectiveNS, effectiveName, effectiveUID, actionRequestID, actionExecutionID sql.NullString
	var identityName, identityUID, identityPodName, identityPodUID sql.NullString
	var delegated [9]sql.NullString
	var wakeAt sql.NullTime
	var commandBytes, admissionBytes, blockerBytes, wakeBytes, serviceBytes []byte
	columns := []any{
		&d.Position, &d.CommandID, &d.SessionID, &d.SandboxName, &d.SandboxUID,
		&d.WakeEligible, &callerNS, &callerName, &incarnation, &wakeAt, &d.Operation,
		&commandBytes, &d.State, &reason, &admissionBytes, &d.Traceparent,
		&actorNS, &actorName, &actorUID, &actorPodName, &actorPodUID,
		&effectiveNS, &effectiveName, &effectiveUID, &actionRequestID, &actionExecutionID,
		&d.CreatedAt, &d.UpdatedAt, &blockerBytes, &wakeBytes, &d.DeliveryAttempted,
		&identityName, &identityUID, &identityPodName, &identityPodUID,
	}
	for i := range delegated {
		columns = append(columns, &delegated[i])
	}
	columns = append(columns, &serviceBytes)
	err := row.Scan(columns...)
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		d.WakeCaller = &auth.Account{callerNS.String, callerName.String}
	}
	if incarnation.Valid {
		d.WakeFromIncarnation = &incarnation.String
	}
	if wakeAt.Valid {
		d.WakeRequestedAt = &wakeAt.Time
	}
	if reason.Valid {
		d.Reason = reason.String
	}
	d.Command = &commandpb.Command{}
	if err := proto.Unmarshal(commandBytes, d.Command); err != nil {
		return nil, err
	}
	if admissionBytes != nil {
		d.Admission = &eventlogpb.EventEntry{}
		if err := proto.Unmarshal(admissionBytes, d.Admission); err != nil {
			return nil, err
		}
	}
	d.DeliveryBlocker, err = unmarshalDeliveryBlocker(blockerBytes)
	if err != nil {
		return nil, err
	}
	d.WakeAssessment, err = unmarshalWakeAssessment(wakeBytes)
	if err != nil {
		return nil, err
	}
	actor, err := scanIdentity(actorNS, actorName, actorUID, "actor")
	if err != nil {
		return nil, err
	}
	effectiveCaller, err := scanIdentity(effectiveNS, effectiveName, effectiveUID, "effective caller")
	if err != nil {
		return nil, err
	}
	account, err := scanAdmissionAttribution(actor, actorPodName, actorPodUID, effectiveCaller, actionRequestID, actionExecutionID, d.WakeCaller)
	if err != nil {
		return nil, err
	}
	d.Attribution, err = scanAttribution(account, identityName, identityUID, identityPodName, identityPodUID)
	if err != nil {
		return nil, err
	}
	delegatedIdentity, err := scanDelegatedIdentityAttribution(delegated)
	if err != nil {
		return nil, err
	}
	if delegatedIdentity != nil {
		if d.Attribution != nil {
			return nil, errors.New("dispatch is attributed to both a delegated Identity and another caller")
		}
		d.Attribution = delegatedIdentity
	}
	d.Attribution, err = scanServiceAttribution(d.Attribution, serviceBytes)
	if err != nil {
		return nil, err
	}
	if d.Attribution == nil && d.WakeCaller == nil {
		return nil, errors.New("dispatch caller attribution is missing")
	}
	return d, nil
}

// EnqueueForCaller applies the Session's retained owner and target binding in the same transaction
// that appends a command, including for Pod-bound privileged backends.
func (s *Store) EnqueueForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *auth.Account, caller Caller) (*Dispatch, error) {
	return s.enqueue(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller, caller)
}

// enqueue appends a command for its authenticated caller.
func (s *Store) enqueue(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *auth.Account, caller Caller) (*Dispatch, error) {
	if command == nil || command.GetCommandId() == "" || sessionID == "" || sandboxName == "" {
		return nil, errors.New("command_id, session_id and sandbox are required")
	}
	if err := ValidateCaller(caller, wakeCaller); err != nil {
		return nil, err
	}
	op, err := operation(command)
	if err != nil {
		return nil, err
	}
	data, err := Wire(command)
	if err != nil {
		return nil, err
	}
	wakeAssessment, err := Wire(initialWakeAssessment())
	if err != nil {
		return nil, err
	}
	var result *Dispatch
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := lockLiveSandbox(ctx, tx, sandboxUID); err != nil {
			return err
		}
		if err := sessionLock(ctx, tx, sessionID); err != nil {
			return err
		}
		query, arguments := sessionForCaller(sessionID, caller)
		session, err := scanSession(tx.QueryRowContext(ctx, query+" FOR SHARE", arguments...))
		if errors.Is(err, sql.ErrNoRows) {
			return ErrNotFound
		}
		if err != nil {
			return err
		}
		if session.SandboxName != sandboxName || session.SandboxUID != sandboxUID {
			return ErrNotAuthorized
		}
		// A command ID is global, even when two senders race through different
		// Session locks.
		if _, err := tx.ExecContext(ctx, "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", "command:"+command.GetCommandId()); err != nil {
			return err
		}
		existing, err := scanDispatch(tx.QueryRowContext(ctx, "SELECT "+dispatchColumns+" FROM dispatch WHERE command_id=$1", command.GetCommandId()))
		if err == nil {
			oldBytes, err := Wire(existing.Command)
			if err != nil {
				return err
			}
			if existing.SessionID != sessionID || existing.SandboxName != sandboxName || !bytes.Equal(oldBytes, data) {
				return ErrCommandConflict
			}
			result = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if session.State == SessionFailedState || session.State == SessionUnknownState {
			return ErrSessionUnconfirmed
		}
		var count int
		err = tx.QueryRowContext(ctx, "SELECT count(*) FROM dispatch WHERE session_id=$1 AND state=$2", sessionID, QueueState).Scan(&count)
		if err == nil && count >= s.QueueCap {
			return fmt.Errorf("%w: session %s holds %d queued commands", ErrQueueFull, sessionID, count)
		}
		if err != nil {
			return err
		}
		var ns, name any
		wakeEligible := wakeCaller != nil
		if wakeEligible {
			ns, name = wakeCaller.Namespace, wakeCaller.Name
		}
		arguments = []any{
			command.GetCommandId(), sessionID, sandboxName, sandboxUID, wakeEligible, ns, name, op, data, QueueState,
			observability.Traceparent(ctx),
		}
		attribution := callerAttribution(caller)
		arguments = append(arguments, attributionValues(attribution)...)
		arguments = append(arguments, nil, wakeAssessment)
		arguments = append(arguments, identityAttributionValues(attribution)...)
		arguments = append(arguments, delegatedIdentityAttributionValues(attribution)...)
		serviceBytes, err := serviceAttributionBytes(attribution)
		if err != nil {
			return err
		}
		arguments = append(arguments, serviceBytes)
		result, err = scanDispatch(tx.QueryRowContext(ctx, `INSERT INTO dispatch
			(command_id,session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
			 operation,command,state,traceparent,actor_account_namespace,actor_account_name,actor_account_uid,actor_pod_name,actor_pod_uid,
			 effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,action_request_id,action_execution_id,
			 created_at,updated_at,delivery_blocker,wake_assessment,
			 actor_identity_name,actor_identity_uid,actor_identity_pod_name,actor_identity_pod_uid,
			 delegated_identity_name,delegated_identity_uid,delegator_account_namespace,delegator_account_name,delegator_account_uid,
			 delegator_pod_name,delegator_pod_uid,delegated_action_request_id,delegated_action_execution_id,service_attribution)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,now(),now(),$22,$23,$24,$25,$26,$27,
			 $28,$29,$30,$31,$32,$33,$34,$35,$36,$37) RETURNING `+dispatchColumns,
			arguments...))
		if err != nil {
			return err
		}
		return notify(ctx, tx, sessionID)
	})
	return result, err
}

func (s *Store) GetDispatch(ctx context.Context, id string) (*Dispatch, error) {
	d, err := scanDispatch(s.DB.QueryRowContext(ctx, "SELECT "+dispatchColumns+" FROM dispatch WHERE command_id=$1", id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return d, err
}

func (s *Store) GetDispatchForCaller(ctx context.Context, id string, caller Caller) (*Dispatch, error) {
	scope, arguments := OwnerScope(caller, "o.", 2)
	query := `SELECT ` + qualifiedDispatchColumns + ` FROM dispatch d
		LEFT JOIN session o ON o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
		WHERE d.command_id=$1 AND ` + scope
	d, err := scanDispatch(s.DB.QueryRowContext(ctx, query, append([]any{id}, arguments...)...))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return d, err
}

// ListDispatchesForCaller returns Dispatches visible to caller in one Session or Sandbox, oldest first.
// A non-empty commandIDs narrows the list to those commands.
func (s *Store) ListDispatchesForCaller(ctx context.Context, sessionID, sandboxName string, commandIDs []string, caller Caller) ([]*Dispatch, error) {
	if (sessionID == "") == (sandboxName == "") {
		return nil, errors.New("exactly one dispatch scope is required")
	}
	column, value := "session_id", sessionID
	if sandboxName != "" {
		column, value = "sandbox_name", sandboxName
	}
	query := `SELECT ` + qualifiedDispatchColumns + ` FROM dispatch d
		LEFT JOIN session o ON o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
		WHERE d.` + column + "=$1"
	arguments := []any{value}
	if len(commandIDs) > 0 {
		query += " AND d.command_id = ANY($2)"
		arguments = append(arguments, pq.Array(commandIDs))
	}
	scope, scopeArguments := OwnerScope(caller, "o.", len(arguments)+1)
	rows, err := s.DB.QueryContext(ctx, query+" AND "+scope+" ORDER BY d.position", append(arguments, scopeArguments...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Dispatch
	for rows.Next() {
		d, err := scanDispatch(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, d)
	}
	return result, rows.Err()
}

func (s *Store) CancelForCaller(ctx context.Context, id string, caller Caller) (*Dispatch, error) {
	var result *Dispatch
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		scope, arguments := OwnerScope(caller, "o.", 5)
		var err error
		result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch d SET state=$1, reason='sender', updated_at=now()
			WHERE d.command_id=$2 AND d.state=$3 AND NOT d.delivery_attempted AND ($4 OR EXISTS (
				SELECT 1 FROM session o WHERE o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
				AND `+scope+`))
			RETURNING `+qualifiedDispatchColumns,
			append([]any{CancelledState, id, QueueState, Privileged(caller)}, arguments...)...))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx, result.SessionID)
	})
	if err != nil {
		return nil, err
	}
	if result != nil {
		return result, nil
	}
	if _, err := s.GetDispatchForCaller(ctx, id, caller); err != nil {
		return nil, err
	}
	return nil, ErrNotCancellable
}

const sessionColumns = `session_id, sandbox_name, sandbox_uid::text, creator_account_namespace,
	creator_account_name, creator_account_uid::text, effective_caller_account_namespace,
	effective_caller_account_name, effective_caller_account_uid, creator_pod_name, creator_pod_uid,
	action_request_id::text, action_execution_id::text, wake_eligible,
	wake_caller_namespace, wake_caller_name, wake_from_incarnation, wake_requested_at,
	spec, setup_script, state, reason, traceparent, created_at, updated_at,
	delivery_blocker, wake_assessment, environment_deleted_at, execution_identity_name, execution_identity_uid::text,
	creator_identity_name, creator_identity_uid, creator_identity_pod_name, creator_identity_pod_uid,
	creator_delegator_account_namespace, creator_delegator_account_name, creator_delegator_account_uid,
	creator_delegator_pod_name, creator_delegator_pod_uid, creator_delegated_action_request_id::text,
	creator_delegated_action_execution_id::text, service_attribution, version, activity, COALESCE((sandbox_uid,activity_epoch) IN (SELECT sandbox_uid,stream_epoch FROM sandbox_activity_watch WHERE holder IS NOT NULL AND expires_at > clock_timestamp()) OR activity_epoch IN (SELECT stream_epoch FROM sandbox_activity_watch WHERE central_sampler AND holder IS NOT NULL AND expires_at > clock_timestamp()), false)`

func scanSession(row interface{ Scan(...any) error }) (*Session, error) {
	o := &Session{}
	var creatorNS, creatorName, creatorUID sql.NullString
	var effectiveNS, effectiveName, effectiveUID, creatorPodName, creatorPodUID sql.NullString
	var actionRequestID, actionExecutionID sql.NullString
	var callerNS, callerName, incarnation, script, reason sql.NullString
	var wakeAt, environmentDeletedAt sql.NullTime
	var identityName, identityUID sql.NullString
	var creatorIdentityName, creatorIdentityUID, creatorIdentityPodName, creatorIdentityPodUID sql.NullString
	var creatorDelegatorNS, creatorDelegatorName, creatorDelegatorUID sql.NullString
	var creatorDelegatorPodName, creatorDelegatorPodUID, creatorActionRequestID, creatorActionExecutionID sql.NullString
	var spec, blockerBytes, wakeBytes, serviceBytes []byte
	err := row.Scan(&o.SessionID, &o.SandboxName, &o.SandboxUID, &creatorNS, &creatorName, &creatorUID,
		&effectiveNS, &effectiveName, &effectiveUID, &creatorPodName, &creatorPodUID,
		&actionRequestID, &actionExecutionID, &o.WakeEligible,
		&callerNS, &callerName, &incarnation, &wakeAt, &spec, &script, &o.State,
		&reason, &o.Traceparent, &o.CreatedAt, &o.UpdatedAt, &blockerBytes, &wakeBytes, &environmentDeletedAt,
		&identityName, &identityUID, &creatorIdentityName, &creatorIdentityUID, &creatorIdentityPodName, &creatorIdentityPodUID,
		&creatorDelegatorNS, &creatorDelegatorName, &creatorDelegatorUID,
		&creatorDelegatorPodName, &creatorDelegatorPodUID, &creatorActionRequestID, &creatorActionExecutionID, &serviceBytes, &o.Version, &o.Activity, &o.ActivityLive)
	if err != nil {
		return nil, err
	}
	if identityName.Valid != identityUID.Valid {
		return nil, errors.New("Session execution identity is incomplete")
	}
	if !identityName.Valid || identityName.String == "" || identityUID.String == "" {
		return nil, errors.New("Session execution identity is missing")
	}
	o.ExecutionIdentity = &identitypb.Identity{Name: identityName.String, Uid: identityUID.String}
	if creatorNS.Valid != creatorName.Valid || creatorNS.Valid != creatorUID.Valid {
		return nil, errors.New("Session creator association is incomplete")
	}
	if creatorNS.Valid {
		o.Creator = serviceAccountIdentityFor(auth.Account{Namespace: creatorNS.String, Name: creatorName.String}, creatorUID.String)
	}
	effectiveCaller, err := scanIdentity(effectiveNS, effectiveName, effectiveUID, "effective caller")
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		o.WakeCaller = &auth.Account{callerNS.String, callerName.String}
	}
	account, err := scanAdmissionAttribution(o.Creator, creatorPodName, creatorPodUID, effectiveCaller, actionRequestID, actionExecutionID, o.WakeCaller)
	if err != nil {
		return nil, err
	}
	o.Attribution, err = scanAttribution(account, creatorIdentityName, creatorIdentityUID, creatorIdentityPodName, creatorIdentityPodUID)
	if err != nil {
		return nil, err
	}
	delegated, err := scanSessionCreatorDelegation(creatorIdentityName, creatorIdentityUID, [7]sql.NullString{
		creatorDelegatorNS, creatorDelegatorName, creatorDelegatorUID,
		creatorDelegatorPodName, creatorDelegatorPodUID, creatorActionRequestID, creatorActionExecutionID,
	})
	if err != nil {
		return nil, err
	}
	if delegated != nil {
		if creatorIdentityPodName.Valid || creatorIdentityPodUID.Valid {
			return nil, errors.New("delegated Identity creator also has direct Pod attribution")
		}
		o.Attribution = delegated
	}
	o.Attribution, err = scanServiceAttribution(o.Attribution, serviceBytes)
	if err != nil {
		return nil, err
	}
	if o.Attribution == nil {
		return nil, errors.New("Session caller attribution is missing")
	}
	if incarnation.Valid {
		o.WakeFromIncarnation = &incarnation.String
	}
	if wakeAt.Valid {
		o.WakeRequestedAt = &wakeAt.Time
	}
	if environmentDeletedAt.Valid {
		o.EnvironmentDeletedAt = &environmentDeletedAt.Time
	}
	if script.Valid {
		o.SetupScript = &script.String
	}
	if reason.Valid {
		o.Reason = reason.String
	}
	o.DeliveryBlocker, err = unmarshalDeliveryBlocker(blockerBytes)
	if err != nil {
		return nil, err
	}
	o.WakeAssessment, err = unmarshalWakeAssessment(wakeBytes)
	if err != nil {
		return nil, err
	}
	o.Spec = &sessionpb.SessionSpec{}
	if err := (proto.UnmarshalOptions{DiscardUnknown: true}).Unmarshal(spec, o.Spec); err != nil {
		return nil, err
	}
	o.Placement = placement.PlacementOf(o.Spec)
	if o.Placement == "" {
		return nil, errors.New("stored Session has no placement")
	}
	target := placement.RequestTarget(&dispatcherpb.CreateSessionRequest{Spec: o.Spec})
	if target.GetName() != o.SandboxName || target.GetUid() != o.SandboxUID {
		return nil, errors.New("stored Session placement disagrees with executor projection")
	}
	return o, nil
}

// CreateSessionWithPlacementForCaller stores the immutable execution account separately from the verified
// caller attribution. Retrying a concurrently inserted session requires retained read authority.
func (s *Store) CreateSessionWithPlacementForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *auth.Account, execution SessionExecution, caller Caller) (*Session, error) {
	return s.CreateSessionWithAdmissionCheck(ctx, sessionID, sandboxName, sandboxUID, placement.PlacementOf(spec), spec, setupScript, wakeCaller, execution, caller, nil)
}

func (s *Store) CreateSessionWithAdmissionCheck(ctx context.Context, sessionID, sandboxName, sandboxUID string, placement placement.SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *auth.Account, execution SessionExecution, caller Caller, check func(context.Context) error) (*Session, error) {
	if callerAttribution(caller) == nil {
		return nil, errors.New("creator identity is required")
	}
	if account, ok := caller.(ServiceAccountCaller); ok && (account.Attribution.Actor == nil ||
		account.Attribution.Actor.GetAccount().GetNamespace() == "" || account.Attribution.Actor.GetAccount().GetName() == "" ||
		account.Attribution.Actor.GetUid() == "") {
		return nil, errors.New("ServiceAccount actor and creator attribution are required")
	}
	if err := ValidateCaller(caller, wakeCaller); err != nil {
		return nil, err
	}
	if identity := execution.Identity; identity != nil && (identity.GetName() == "" || identity.GetUid() == "") {
		return nil, errors.New("execution identity requires a name and UID")
	}
	return s.openSession(ctx, sessionID, sandboxName, sandboxUID, placement, spec, setupScript, wakeCaller, execution, caller, check)
}

// authorizeRetry uses retained read authority: a receipt retry performs no new execution.
func (o *Session) authorizeRetry(caller Caller) error {
	switch c := caller.(type) {
	case ServiceCaller:
		return nil
	case ServiceAccountCaller:
		if c.Privileged {
			return nil
		}
	case IdentityCaller:
		if SameIdentity(o.ExecutionIdentity, c.Identity) {
			return nil
		}
	}
	return ErrNotFound
}

// AuthorizeCreateRetry adds exact creator recovery for Identity-created Sessions. It is used only
// by CreateSession receipt recovery and the transactional duplicate path; ordinary Session reads
// continue to authorize against the immutable execution Identity.
func (o *Session) AuthorizeCreateRetry(caller Caller) error {
	switch c := caller.(type) {
	case ServiceCaller, ServiceAccountCaller:
		return o.authorizeRetry(caller)
	case IdentityCaller:
		var creator *identitypb.Identity
		switch attribution := o.Attribution.(type) {
		case *IdentityAttribution:
			creator = attribution.Identity
		case *DelegatedIdentityAttribution:
			creator = attribution.Identity
		case *ServiceAttribution:
			creator = attribution.Identity
		}
		if creator != nil && SameIdentity(creator, c.Identity) {
			return nil
		}
	}
	return ErrNotFound
}

func (o *Session) MatchRequest(sandboxName, sandboxUID string, placement placement.SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string) error {
	if spec == nil {
		return ErrSessionConflict
	}
	oldBytes, err := Wire(o.Spec)
	if err != nil {
		return err
	}
	data, err := Wire(spec)
	if err != nil {
		return err
	}
	if o.SandboxName != sandboxName || o.SandboxUID != sandboxUID || o.Placement != placement || !bytes.Equal(oldBytes, data) || !ptr.Equal(o.SetupScript, setupScript) {
		return ErrSessionConflict
	}
	return nil
}

func (s *Store) openSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, sessionPlacement placement.SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *auth.Account, execution SessionExecution, caller Caller, check func(context.Context) error) (*Session, error) {
	if sessionID == "" || sandboxName == "" || spec == nil {
		return nil, errors.New("session_id, sandbox and spec are required")
	}
	if execution.Identity == nil || execution.Identity.GetName() == "" || execution.Identity.GetUid() == "" {
		return nil, errors.New("new Session requires a complete execution Identity")
	}
	if err := ValidateCaller(caller, wakeCaller); err != nil {
		return nil, err
	}
	if !sessionPlacement.MatchesSpec(spec) {
		return nil, errors.New("Session placement does not match spec")
	}
	target := placement.RequestTarget(&dispatcherpb.CreateSessionRequest{Spec: spec})
	if target.GetName() != sandboxName || target.GetUid() != sandboxUID {
		return nil, errors.New("Session placement does not match executor projection")
	}
	data, err := Wire(spec)
	if err != nil {
		return nil, err
	}
	wakeAssessment, err := Wire(initialWakeAssessment())
	if err != nil {
		return nil, err
	}
	var result *Session
	err = transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := lockLiveSandbox(ctx, tx, sandboxUID); err != nil {
			return err
		}
		if err := sessionLock(ctx, tx, sessionID); err != nil {
			return err
		}
		existing, err := scanSession(tx.QueryRowContext(ctx, "SELECT "+sessionColumns+" FROM session WHERE session_id=$1", sessionID))
		if err == nil {
			if err := existing.AuthorizeCreateRetry(caller); err != nil {
				return err
			}
			if err := existing.MatchRequest(sandboxName, sandboxUID, sessionPlacement, spec, setupScript); err != nil {
				return err
			}
			result = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		if check != nil {
			if err := check(ctx); err != nil {
				return err
			}
		}
		var ns, name any
		if wakeCaller != nil {
			ns, name = wakeCaller.Namespace, wakeCaller.Name
		}
		var creatorNS, creatorName, creatorUID, identityName, identityUID any
		if execution.Identity != nil {
			identityName, identityUID = execution.Identity.GetName(), execution.Identity.GetUid()
		}
		attribution := callerAttribution(caller)
		if account, ok := attribution.(*AdmissionAttribution); ok {
			creator := account.Actor
			creatorNS, creatorName, creatorUID = creator.GetAccount().GetNamespace(), creator.GetAccount().GetName(), creator.GetUid()
		}
		attributionArgs := attributionValues(attribution)
		identityArgs := sessionIdentityAttributionValues(attribution)
		delegationArgs := sessionDelegationAttributionValues(attribution)
		serviceBytes, err := serviceAttributionBytes(attribution)
		if err != nil {
			return err
		}
		result, err = scanSession(tx.QueryRowContext(ctx, `INSERT INTO session
			(session_id,sandbox_name,sandbox_uid,creator_account_namespace,creator_account_name,creator_account_uid,effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
			 creator_pod_name,creator_pod_uid,action_request_id,action_execution_id,wake_eligible,wake_caller_namespace,wake_caller_name,
			 spec,setup_script,state,traceparent,created_at,updated_at,delivery_blocker,wake_assessment,
			 execution_identity_name,execution_identity_uid,
			 creator_identity_name,creator_identity_uid,creator_identity_pod_name,creator_identity_pod_uid,
			 creator_delegator_account_namespace,creator_delegator_account_name,creator_delegator_account_uid,
			 creator_delegator_pod_name,creator_delegator_pod_uid,creator_delegated_action_request_id,creator_delegated_action_execution_id,service_attribution)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,now(),now(),$21,$22,$23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34,$35,$36) RETURNING `+sessionColumns,
			sessionID, sandboxName, sandboxUID, creatorNS, creatorName, creatorUID,
			attributionArgs[5], attributionArgs[6], attributionArgs[7], attributionArgs[3], attributionArgs[4], attributionArgs[8], attributionArgs[9],
			wakeCaller != nil, ns, name, data, setupScript, SessionWaitingState,
			observability.Traceparent(ctx), nil, wakeAssessment, identityName, identityUID,
			identityArgs[0], identityArgs[1], identityArgs[2], identityArgs[3],
			delegationArgs[0], delegationArgs[1], delegationArgs[2], delegationArgs[3], delegationArgs[4], delegationArgs[5], delegationArgs[6], serviceBytes))
		if err != nil {
			return err
		}
		return notify(ctx, tx, sessionID)
	})
	return result, err
}

func (s *Store) GetSession(ctx context.Context, id string) (*Session, error) {
	o, err := scanSession(s.DB.QueryRowContext(ctx, "SELECT "+sessionColumns+" FROM session WHERE session_id=$1", id))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return o, err
}

// sessionForCaller is the query reading Session id if caller may read it.
func sessionForCaller(id string, caller Caller) (string, []any) {
	scope, arguments := OwnerScope(caller, "", 2)
	return "SELECT " + sessionColumns + " FROM session WHERE session_id=$1 AND " + scope, append([]any{id}, arguments...)
}

func (s *Store) GetSessionForCaller(ctx context.Context, id string, caller Caller) (*Session, error) {
	query, arguments := sessionForCaller(id, caller)
	session, err := scanSession(s.DB.QueryRowContext(ctx, query, arguments...))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return session, err
}

func (s *Store) ListSessionsForCaller(ctx context.Context, after string, limit int, caller Caller) ([]*Session, error) {
	scope, arguments := OwnerScope(caller, "", 3)
	rows, err := s.DB.QueryContext(ctx, `SELECT `+sessionColumns+` FROM session
		WHERE session_id > $1 AND `+scope+` ORDER BY session_id LIMIT $2`, append([]any{after, limit}, arguments...)...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Session
	for rows.Next() {
		row, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

// SessionHead reads the durable head after a queue key becomes runnable. The
// in-process queue never decides ordering or whether a Session is complete.
func (s *Store) SessionHead(ctx context.Context, sessionID string) (*Session, *Dispatch, error) {
	session, err := scanSession(s.DB.QueryRowContext(ctx,
		"SELECT "+sessionColumns+" FROM session WHERE session_id=$1 AND state=$2", sessionID, SessionWaitingState))
	if err == nil {
		return session, nil, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return nil, nil, err
	}
	head, err := scanDispatch(s.DB.QueryRowContext(ctx, `SELECT `+dispatchColumns+` FROM dispatch d WHERE d.session_id=$1 AND d.state=$2
		AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.session_id=d.session_id
			AND earlier.position<d.position AND earlier.state IN ($2,$3))
		AND NOT EXISTS (SELECT 1 FROM session o WHERE o.session_id=d.session_id
			AND o.state IN ($4,$5)) ORDER BY d.position LIMIT 1`, sessionID, QueueState, DeliveringState, SessionWaitingState, SessionInProgressState))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil, nil
	}
	return nil, head, err
}

// WakeCommands includes cancelled work that can still have an external wake in flight.
func (s *Store) WakeCommands(ctx context.Context, sessionID, sandboxName, sandboxUID string) ([]*Dispatch, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT "+dispatchColumns+` FROM dispatch
		WHERE session_id=$1 AND sandbox_name=$2 AND sandbox_uid=$3
		AND (state=$4 OR wake_from_incarnation IS NOT NULL) ORDER BY position`, sessionID, sandboxName, sandboxUID, QueueState)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Dispatch
	for rows.Next() {
		row, err := scanDispatch(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

// SessionsWithPendingWork includes stranded in-flight work even when no lease row exists.
func (s *Store) SessionsWithPendingWork(ctx context.Context) ([]*Session, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+sessionColumns+` FROM session o
		WHERE o.state = ANY($1) OR EXISTS (
			SELECT 1 FROM dispatch d WHERE d.session_id=o.session_id AND d.state = ANY($2))`,
		pq.Array([]string{SessionWaitingState, SessionInProgressState}), pq.Array([]string{QueueState, DeliveringState}))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Session
	for rows.Next() {
		row, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, row)
	}
	return result, rows.Err()
}

// MarkDeliveryAttempt commits possible forwarding before issuing the external RPC.
// Requeue, restart and lease handoff must never turn it back into known-unsent work.
func (s *Store) MarkDeliveryAttempt(ctx context.Context, id string, owner SessionOwner) (bool, error) {
	marked := false
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE dispatch SET delivery_attempted=true
			WHERE command_id=$1 AND session_id=$2 AND state=$3`, id, owner.SessionID, DeliveringState)
		if err != nil {
			return err
		}
		count, err := result.RowsAffected()
		marked = count == 1
		return err
	})
	return marked, err
}

func (s *Store) Settle(ctx context.Context, id string, owner SessionOwner, from []string, to, reason string, admission *eventlogpb.EventEntry) (*Dispatch, error) {
	var result *Dispatch
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		// A nil `[]byte` stores an empty bytea, which reads back as an empty admission; only a nil
		// interface stores NULL.
		var payload any
		if admission != nil {
			payload, err = Wire(admission)
			if err != nil {
				return err
			}
		}
		result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=CASE WHEN $3='FAILED' AND state='QUEUED_IN_DISPATCHER' AND delivery_attempted THEN 'UNKNOWN' ELSE $3 END, reason=$4, admission=$5, updated_at=now()
			WHERE command_id=$1 AND session_id=$2 AND state = ANY($6) RETURNING `+dispatchColumns,
			id, owner.SessionID, to, nullIfEmpty(reason), payload, pq.Array(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if result != nil {
			return notify(ctx, tx, owner.SessionID)
		}
		return nil
	})
	return result, err
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (s *Store) SettleSession(ctx context.Context, id string, owner SessionOwner, from []string, to, reason string) (*Session, error) {
	return s.settleSession(ctx, id, owner, from, to, reason, false)
}

// RejectSession records a proven placement-owner rejection and fails only commands that have
// not left the queue. An older FAILED row alone cannot supply this evidence.
func (s *Store) RejectSession(ctx context.Context, id string, owner SessionOwner, from []string, reason string) (*Session, error) {
	return s.settleSession(ctx, id, owner, from, SessionFailedState, reason, true)
}

func (s *Store) settleSession(ctx context.Context, id string, owner SessionOwner, from []string, to, reason string, rejectQueued bool) (*Session, error) {
	var result *Session
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		// ownerMatch takes admission's Session lock before the lease row, so
		// enqueue either precedes rejection or observes its terminal outcome.
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err = scanSession(tx.QueryRowContext(ctx, `UPDATE session SET state=$3,reason=$4,updated_at=now()
			WHERE session_id=$1 AND session_id=$2 AND state = ANY($5) RETURNING `+sessionColumns,
			id, owner.SessionID, to, nullIfEmpty(reason), pq.Array(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if rejectQueued {
			if _, err := tx.ExecContext(ctx, `UPDATE dispatch SET state=CASE WHEN delivery_attempted THEN 'UNKNOWN' ELSE $3 END,reason='session_rejected',updated_at=now()
				WHERE session_id=$1 AND session_id=$2 AND state=$4`, id, owner.SessionID, FailedState, QueueState); err != nil {
				return err
			}
		}
		return notify(ctx, tx, owner.SessionID)
	})
	return result, err
}

// RecoverSession requeues only this Session's uncertain in-flight work under its lease.
// Runtime command/session IDs make replay safe after a lost reply.
func (s *Store) RecoverSession(ctx context.Context, owner SessionOwner) (bool, error) {
	current := false
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		current, err = ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		dispatches, err := tx.ExecContext(ctx, `UPDATE dispatch SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE session_id=$1 AND state=$3`, owner.SessionID, QueueState, DeliveringState)
		if err != nil {
			return err
		}
		sessions, err := tx.ExecContext(ctx, `UPDATE session SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE session_id=$1 AND state=$3`, owner.SessionID, SessionWaitingState, SessionInProgressState)
		if err != nil {
			return err
		}
		dispatchCount, err := dispatches.RowsAffected()
		if err != nil {
			return err
		}
		sessionCount, err := sessions.RowsAffected()
		if err != nil || dispatchCount+sessionCount == 0 {
			return err
		}
		return notify(ctx, tx, owner.SessionID)
	})
	return current, err
}

// NoteWaiting changes only a still-queued row under the current Session lease.
func (s *Store) NoteWaiting(ctx context.Context, id string, owner SessionOwner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE dispatch SET reason=$3,updated_at=now()
			WHERE command_id=$1 AND session_id=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SessionID, reason, QueueState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx, owner.SessionID)
		}
		return nil
	})
}

func (s *Store) NoteSessionWaiting(ctx context.Context, id string, owner SessionOwner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE session SET reason=$3,updated_at=now()
			WHERE session_id=$1 AND session_id=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SessionID, reason, SessionWaitingState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx, owner.SessionID)
		}
		return nil
	})
}

// SetDeliveryAssessment stores the typed blocker and latest wake observation on a queued receipt.
// The protobuf bytes and PostgreSQL notification commit together so every WatchDispatches replica
// can reread the updated facts even when DeliveryState and reason are unchanged.
func (s *Store) SetDeliveryAssessment(
	ctx context.Context,
	id string,
	owner SessionOwner,
	session bool,
	blocker *environmentpb.DeliveryBlocker,
	assessment *environmentpb.WakeAssessment,
) error {
	if assessment == nil {
		return errors.New("wake assessment is required")
	}
	blockerBytes, err := wireOptional(blocker)
	if err != nil {
		return err
	}
	assessmentBytes, err := Wire(assessment)
	if err != nil {
		return err
	}
	table, key := wakeTable(session)
	waiting := waitingState(session)
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE `+table+` SET delivery_blocker=$3,wake_assessment=$4,updated_at=now()
			WHERE `+key+`=$1 AND session_id=$2 AND state=$5
			AND (delivery_blocker IS DISTINCT FROM $3 OR wake_assessment IS DISTINCT FROM $4)`,
			id, owner.SessionID, blockerBytes, assessmentBytes, waiting)
		if err != nil {
			return err
		}
		changed, err := result.RowsAffected()
		if err != nil || changed == 0 {
			return err
		}
		return notify(ctx, tx, owner.SessionID)
	})
}

// SandboxSessions limits a catalog invalidation to the affected incarnation.
func (s *Store) SandboxSessions(ctx context.Context, sandboxUID string) ([]string, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT session_id FROM session WHERE sandbox_uid=$1 AND environment_deleted_at IS NULL`, sandboxUID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}
