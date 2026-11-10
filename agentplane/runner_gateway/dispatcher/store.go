// Package dispatcher stores accepted Session work before delivery. PostgreSQL is
// both the queue and the record of delivery; notifications are only invalidations.
package dispatcher

import (
	"bytes"
	"context"
	"database/sql"
	"errors"
	"fmt"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/postgres"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	environmentpb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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

type Account = auth.Account

type ServiceAccountIdentity = identitypb.ServiceAccountIdentity

func serviceAccountIdentityFor(account Account, uid string) *ServiceAccountIdentity {
	return &ServiceAccountIdentity{
		Account: &identitypb.ServiceAccount{Namespace: account.Namespace, Name: account.Name}, Uid: uid,
	}
}

func accountOfIdentity(identity *ServiceAccountIdentity) Account {
	if identity == nil || identity.GetAccount() == nil {
		return Account{}
	}
	return Account{Namespace: identity.GetAccount().GetNamespace(), Name: identity.GetAccount().GetName()}
}

func uidOfIdentity(identity *ServiceAccountIdentity) string {
	if identity == nil {
		return ""
	}
	return identity.GetUid()
}

func sameServiceAccountIdentity(left, right *ServiceAccountIdentity) bool {
	if left == nil || right == nil {
		return left == right
	}
	return sameServiceAccount(left.GetAccount(), right.GetAccount()) && left.GetUid() == right.GetUid()
}

// sameIdentity compares Identity incarnations, nil meaning none.
func sameIdentity(left, right *identitypb.Identity) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.GetName() == right.GetName() && left.GetUid() == right.GetUid()
}

func sameServiceAccount(left, right *identitypb.ServiceAccount) bool {
	if left == nil || right == nil {
		return left == right
	}
	return left.GetNamespace() == right.GetNamespace() && left.GetName() == right.GetName()
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
	Actor           *ServiceAccountIdentity
	ActorPod        *PodIdentity
	EffectiveCaller *ServiceAccountIdentity
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

// Attribution is the verified caller recorded with a row: an *AdmissionAttribution for a
// ServiceAccount caller or an *IdentityAttribution.
type Attribution interface{ isAttribution() }

// Caller is an admitted Dispatcher caller: a ServiceAccountCaller or an IdentityCaller.
type Caller interface{ isCaller() }

// ServiceAccountCaller distinguishes the caller's actual ServiceAccount from the explicit, Pod-bound
// backend path that retains the existing app/notification/Actions authority.
type ServiceAccountCaller struct {
	ServiceAccountIdentity *ServiceAccountIdentity
	Privileged             bool
	Attribution            *AdmissionAttribution
}

// IdentityCaller is a workload whose SVID named an Identity. It is never privileged: it owns the
// Sessions whose execution identity is its Identity incarnation, name and UID.
type IdentityCaller struct {
	Identity *identitypb.Identity
	Pod      *PodIdentity
}

func (ServiceAccountCaller) isCaller() {}
func (IdentityCaller) isCaller()       {}

func privileged(caller Caller) bool {
	account, ok := caller.(ServiceAccountCaller)
	return ok && account.Privileged
}

// callerAttribution is the Attribution a row records for caller, nil for an internal write.
func callerAttribution(caller Caller) Attribution {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		if c.Attribution == nil {
			return nil
		}
		return c.Attribution
	case IdentityCaller:
		return &IdentityAttribution{Identity: c.Identity, Pod: c.Pod}
	default:
		return nil
	}
}

// validateCaller checks the attribution caller will record.
func validateCaller(caller Caller, wakeCaller *Account) error {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		return validateAttribution(c.Attribution, wakeCaller)
	case IdentityCaller:
		if c.Identity.GetName() == "" || c.Identity.GetUid() == "" {
			return errors.New("Identity caller requires a name and UID")
		}
		if c.Pod != nil && (c.Pod.Name == "" || c.Pod.UID == "") {
			return errors.New("Identity caller has incomplete Pod provenance")
		}
		if wakeCaller != nil {
			return errors.New("an Identity caller is never a wake caller")
		}
		return nil
	default:
		return nil
	}
}

// ownerScope is the SQL condition limiting Session rows (columns qualified by prefix) to those caller
// may read, with its arguments numbered from next.
func ownerScope(caller Caller, prefix string, next int) (string, []any) {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		account := accountOfIdentity(c.ServiceAccountIdentity)
		return fmt.Sprintf("($%[1]d OR (%[5]sexecution_account_namespace=$%[2]d AND %[5]sexecution_account_name=$%[3]d AND %[5]sexecution_account_uid=$%[4]d))",
				next, next+1, next+2, next+3, prefix),
			[]any{c.Privileged, account.Namespace, account.Name, uidOfIdentity(c.ServiceAccountIdentity)}
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
	WakeCaller              *Account
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
	SessionID, SandboxName, SandboxUID string
	Placement                          SessionPlacement
	// ExecutionPrincipal is the immutable logical owner of the Session. A nil account with a nil
	// ExecutionIdentity means a legacy row with no trustworthy owner; an empty UID with a set account
	// is a privileged open waiting to bind the controller-owned ServiceAccount.
	ExecutionPrincipal *ServiceAccountIdentity
	// ExecutionIdentity is the Identity the Session's Sandbox named in `spec.identity` when the
	// Session was created, nil when it named none. It is immutable. Beside a nil ExecutionPrincipal it
	// is an Identity-run Session: its Sandbox had no managed ServiceAccount, and delivery checks only
	// that the Sandbox still names this Identity.
	ExecutionIdentity *identitypb.Identity
	// Creator records the authenticated actor that brokered the session, separately from its owner
	// and effective caller. For a delegated session this is the Actions ServiceAccount.
	Creator              *ServiceAccountIdentity
	Attribution          Attribution
	WakeEligible         bool
	WakeCaller           *Account
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

// Owner is a fresh Kubernetes Lease proof paired with the committed DB generation.
// Every worker transition locks and compares this full token before changing work.
type Owner struct {
	SandboxUID, LeaseUID, HolderIdentity string
	Generation                           int64
	LeaseTransitions                     int
}

type Store struct {
	DB       *sql.DB
	QueueCap int
}

func validateAttribution(attribution *AdmissionAttribution, wakeCaller *Account) error {
	if attribution == nil {
		return nil
	}
	for _, identity := range []*ServiceAccountIdentity{attribution.Actor, attribution.EffectiveCaller} {
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

func scanIdentity(namespace, name, uid sql.NullString, field string) (*ServiceAccountIdentity, error) {
	if namespace.Valid != name.Valid || namespace.Valid != uid.Valid {
		return nil, fmt.Errorf("%s attribution is incomplete", field)
	}
	if !namespace.Valid {
		return nil, nil
	}
	return serviceAccountIdentityFor(Account{Namespace: namespace.String, Name: name.String}, uid.String), nil
}

func scanAdmissionAttribution(
	actor *ServiceAccountIdentity,
	actorPodName, actorPodUID sql.NullString,
	effectiveCaller *ServiceAccountIdentity,
	requestID, executionID sql.NullString,
	wakeCaller *Account,
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
	if err := validateAttribution(attribution, wakeCaller); err != nil {
		return nil, err
	}
	return attribution, nil
}

func NewStore(db *sql.DB, queueCap int) *Store { return &Store{DB: db, QueueCap: queueCap} }

func transaction(ctx context.Context, db *sql.DB, f func(*sql.Tx) error) error {
	return postgres.Transaction(ctx, db, sql.TxOptions{}, f)
}

func notify(ctx context.Context, tx *sql.Tx) error {
	return postgres.Notify(ctx, tx, NotifyChannel, "")
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

func wire(message proto.Message) ([]byte, error) {
	return proto.MarshalOptions{Deterministic: true}.Marshal(message)
}

func wireOptional(message proto.Message) ([]byte, error) {
	if message == nil {
		return nil, nil
	}
	return wire(message)
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
	actor_identity_name, actor_identity_uid, actor_identity_pod_name, actor_identity_pod_uid`

const qualifiedDispatchColumns = `d.position, d.command_id, d.session_id, d.sandbox_name, d.sandbox_uid::text,
	d.wake_eligible, d.wake_caller_namespace, d.wake_caller_name, d.wake_from_incarnation,
	d.wake_requested_at, d.operation, d.command, d.state, d.reason, d.admission, d.traceparent,
	d.actor_account_namespace, d.actor_account_name, d.actor_account_uid, d.actor_pod_name, d.actor_pod_uid,
	d.effective_caller_account_namespace, d.effective_caller_account_name, d.effective_caller_account_uid,
	d.action_request_id::text, d.action_execution_id::text, d.created_at, d.updated_at,
	d.delivery_blocker, d.wake_assessment, d.delivery_attempted,
	d.actor_identity_name, d.actor_identity_uid, d.actor_identity_pod_name, d.actor_identity_pod_uid`

func scanDispatch(row interface{ Scan(...any) error }) (*Dispatch, error) {
	return scanDispatchColumns(row)
}

func scanDispatchWithOwner(row interface{ Scan(...any) error }) (*Dispatch, *ServiceAccountIdentity, *identitypb.Identity, error) {
	var ownerNS, ownerName, ownerUID, identityName, identityUID sql.NullString
	dispatch, err := scanDispatchColumns(row, &ownerNS, &ownerName, &ownerUID, &identityName, &identityUID)
	if err != nil {
		return nil, nil, nil, err
	}
	if ownerNS.Valid != ownerName.Valid || ownerUID.Valid && !ownerNS.Valid || identityName.Valid != identityUID.Valid {
		return nil, nil, nil, errors.New("terminal Session principal association is incomplete")
	}
	var ownerIdentity *identitypb.Identity
	if identityName.Valid {
		ownerIdentity = &identitypb.Identity{Name: identityName.String, Uid: identityUID.String}
	}
	var owner *ServiceAccountIdentity
	if ownerNS.Valid {
		owner = serviceAccountIdentityFor(Account{Namespace: ownerNS.String, Name: ownerName.String}, "")
		if ownerUID.Valid {
			owner.Uid = ownerUID.String
		}
	}
	return dispatch, owner, ownerIdentity, nil
}

func scanDispatchColumns(row interface{ Scan(...any) error }, additional ...any) (*Dispatch, error) {
	d := &Dispatch{}
	var callerNS, callerName, incarnation, reason sql.NullString
	var actorNS, actorName, actorUID, actorPodName, actorPodUID sql.NullString
	var effectiveNS, effectiveName, effectiveUID, actionRequestID, actionExecutionID sql.NullString
	var identityName, identityUID, identityPodName, identityPodUID sql.NullString
	var wakeAt sql.NullTime
	var commandBytes, admissionBytes, blockerBytes, wakeBytes []byte
	columns := []any{
		&d.Position, &d.CommandID, &d.SessionID, &d.SandboxName, &d.SandboxUID,
		&d.WakeEligible, &callerNS, &callerName, &incarnation, &wakeAt, &d.Operation,
		&commandBytes, &d.State, &reason, &admissionBytes, &d.Traceparent,
		&actorNS, &actorName, &actorUID, &actorPodName, &actorPodUID,
		&effectiveNS, &effectiveName, &effectiveUID, &actionRequestID, &actionExecutionID,
		&d.CreatedAt, &d.UpdatedAt, &blockerBytes, &wakeBytes, &d.DeliveryAttempted,
		&identityName, &identityUID, &identityPodName, &identityPodUID,
	}
	columns = append(columns, additional...)
	err := row.Scan(columns...)
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		d.WakeCaller = &Account{callerNS.String, callerName.String}
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
	return d, nil
}

// Enqueue commits the complete command and NOTIFY in one transaction. Exact retries
// return the original row after verifying the retained Session and exact Sandbox UID.
func (s *Store) Enqueue(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *Account) (*Dispatch, error) {
	return s.enqueue(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller, nil)
}

// EnqueueForCaller applies the Session's retained owner and target binding in the same transaction
// that appends a command, including for Pod-bound privileged backends.
func (s *Store) EnqueueForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *Account, caller Caller) (*Dispatch, error) {
	return s.enqueue(ctx, sessionID, sandboxName, sandboxUID, command, wakeCaller, caller)
}

// enqueue appends a command; a nil caller is an internal write.
func (s *Store) enqueue(ctx context.Context, sessionID, sandboxName, sandboxUID string, command *commandpb.Command, wakeCaller *Account, caller Caller) (*Dispatch, error) {
	if command == nil || command.GetCommandId() == "" || sessionID == "" || sandboxName == "" {
		return nil, errors.New("command_id, session_id and sandbox are required")
	}
	if caller != nil {
		if err := validateCaller(caller, wakeCaller); err != nil {
			return nil, err
		}
	}
	op, err := operation(command)
	if err != nil {
		return nil, err
	}
	data, err := wire(command)
	if err != nil {
		return nil, err
	}
	wakeAssessment, err := wire(initialWakeAssessment())
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
		query := "SELECT " + sessionColumns + " FROM session WHERE session_id=$1"
		arguments := []any{sessionID}
		if caller != nil {
			query, arguments = sessionForCaller(sessionID, caller)
		}
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
			oldBytes, err := wire(existing.Command)
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
		if session.Placement.requiresReadyExecutor() && (session.State == SessionFailedState || session.State == SessionUnknownState) {
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
		result, err = scanDispatch(tx.QueryRowContext(ctx, `INSERT INTO dispatch
			(command_id,session_id,sandbox_name,sandbox_uid,wake_eligible,wake_caller_namespace,wake_caller_name,
			 operation,command,state,traceparent,actor_account_namespace,actor_account_name,actor_account_uid,actor_pod_name,actor_pod_uid,
			 effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,action_request_id,action_execution_id,
			 created_at,updated_at,delivery_blocker,wake_assessment,
			 actor_identity_name,actor_identity_uid,actor_identity_pod_name,actor_identity_pod_uid)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,now(),now(),$22,$23,$24,$25,$26,$27) RETURNING `+dispatchColumns,
			arguments...))
		if err != nil {
			return err
		}
		return notify(ctx, tx)
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
	scope, arguments := ownerScope(caller, "o.", 2)
	query := `SELECT ` + qualifiedDispatchColumns + ` FROM dispatch d
		LEFT JOIN session o ON o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
		WHERE d.command_id=$1 AND ` + scope
	d, err := scanDispatch(s.DB.QueryRowContext(ctx, query, append([]any{id}, arguments...)...))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, ErrNotFound
	}
	return d, err
}

// ListDispatches returns the Dispatches of one Session or Sandbox, oldest first; a non-empty commandIDs
// narrows them to those commands.
func (s *Store) ListDispatches(ctx context.Context, sessionID, sandboxName string, commandIDs []string) ([]*Dispatch, error) {
	return s.listDispatches(ctx, sessionID, sandboxName, commandIDs, nil)
}

func (s *Store) ListDispatchesForCaller(ctx context.Context, sessionID, sandboxName string, commandIDs []string, caller Caller) ([]*Dispatch, error) {
	return s.listDispatches(ctx, sessionID, sandboxName, commandIDs, caller)
}

func (s *Store) listDispatches(ctx context.Context, sessionID, sandboxName string, commandIDs []string, caller Caller) ([]*Dispatch, error) {
	if (sessionID == "") == (sandboxName == "") {
		return nil, errors.New("exactly one dispatch scope is required")
	}
	column, value := "session_id", sessionID
	if sandboxName != "" {
		column, value = "sandbox_name", sandboxName
	}
	columns := dispatchColumns
	query, arguments := "SELECT "+columns+" FROM dispatch d", []any{}
	if caller != nil {
		query = "SELECT " + qualifiedDispatchColumns + " FROM dispatch d"
		query += " LEFT JOIN session o ON o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name"
	}
	query += " WHERE d." + column + "=$1"
	arguments = append(arguments, value)
	if len(commandIDs) > 0 {
		query += " AND d.command_id = ANY($2)"
		arguments = append(arguments, pq.Array(commandIDs))
	}
	if caller != nil {
		scope, scopeArguments := ownerScope(caller, "o.", len(arguments)+1)
		query += " AND " + scope
		arguments = append(arguments, scopeArguments...)
	}
	rows, err := s.DB.QueryContext(ctx, query+" ORDER BY d.position", arguments...)
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

func (s *Store) Cancel(ctx context.Context, id string) (*Dispatch, error) {
	return s.cancel(ctx, id, nil)
}

func (s *Store) CancelForCaller(ctx context.Context, id string, caller Caller) (*Dispatch, error) {
	return s.cancel(ctx, id, caller)
}

func (s *Store) cancel(ctx context.Context, id string, caller Caller) (*Dispatch, error) {
	var result *Dispatch
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		if caller == nil {
			result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=$1, reason='sender', updated_at=now()
				WHERE command_id=$2 AND state=$3 AND NOT delivery_attempted RETURNING `+dispatchColumns, CancelledState, id, QueueState))
		} else {
			scope, arguments := ownerScope(caller, "o.", 5)
			result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch d SET state=$1, reason='sender', updated_at=now()
				WHERE d.command_id=$2 AND d.state=$3 AND NOT d.delivery_attempted AND ($4 OR EXISTS (
					SELECT 1 FROM session o WHERE o.session_id=d.session_id AND o.sandbox_uid=d.sandbox_uid AND o.sandbox_name=d.sandbox_name
					AND `+scope+`))
				RETURNING `+qualifiedDispatchColumns,
				append([]any{CancelledState, id, QueueState, privileged(caller)}, arguments...)...))
		}
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	if err != nil {
		return nil, err
	}
	if result != nil {
		return result, nil
	}
	if caller == nil {
		if _, err = s.GetDispatch(ctx, id); err != nil {
			return nil, err
		}
	} else {
		if _, err = s.GetDispatchForCaller(ctx, id, caller); err != nil {
			return nil, err
		}
	}
	return nil, ErrNotCancellable
}

const sessionColumns = `session_id, sandbox_name, sandbox_uid::text, execution_account_namespace,
	execution_account_name, execution_account_uid::text, creator_account_namespace,
	creator_account_name, creator_account_uid::text, effective_caller_account_namespace,
	effective_caller_account_name, effective_caller_account_uid, creator_pod_name, creator_pod_uid,
	action_request_id::text, action_execution_id::text, wake_eligible,
	wake_caller_namespace, wake_caller_name, wake_from_incarnation, wake_requested_at,
	spec, setup_script, state, reason, traceparent, created_at, updated_at,
	delivery_blocker, wake_assessment, environment_deleted_at, execution_identity_name, execution_identity_uid::text,
	creator_identity_name, creator_identity_uid, creator_identity_pod_name, creator_identity_pod_uid`

func scanSession(row interface{ Scan(...any) error }) (*Session, error) {
	o := &Session{}
	var ownerNS, ownerName, ownerUID, creatorNS, creatorName, creatorUID sql.NullString
	var effectiveNS, effectiveName, effectiveUID, creatorPodName, creatorPodUID sql.NullString
	var actionRequestID, actionExecutionID sql.NullString
	var callerNS, callerName, incarnation, script, reason sql.NullString
	var wakeAt, environmentDeletedAt sql.NullTime
	var identityName, identityUID sql.NullString
	var creatorIdentityName, creatorIdentityUID, creatorIdentityPodName, creatorIdentityPodUID sql.NullString
	var spec, blockerBytes, wakeBytes []byte
	err := row.Scan(&o.SessionID, &o.SandboxName, &o.SandboxUID, &ownerNS, &ownerName, &ownerUID,
		&creatorNS, &creatorName, &creatorUID, &effectiveNS, &effectiveName, &effectiveUID, &creatorPodName, &creatorPodUID,
		&actionRequestID, &actionExecutionID, &o.WakeEligible,
		&callerNS, &callerName, &incarnation, &wakeAt, &spec, &script, &o.State,
		&reason, &o.Traceparent, &o.CreatedAt, &o.UpdatedAt, &blockerBytes, &wakeBytes, &environmentDeletedAt,
		&identityName, &identityUID, &creatorIdentityName, &creatorIdentityUID, &creatorIdentityPodName, &creatorIdentityPodUID)
	if err != nil {
		return nil, err
	}
	if identityName.Valid != identityUID.Valid {
		return nil, errors.New("Session execution identity is incomplete")
	}
	if identityName.Valid {
		o.ExecutionIdentity = &identitypb.Identity{Name: identityName.String, Uid: identityUID.String}
	}
	if ownerNS.Valid != ownerName.Valid || ownerUID.Valid && !ownerNS.Valid || creatorNS.Valid != creatorName.Valid ||
		creatorNS.Valid != creatorUID.Valid {
		return nil, errors.New("Session principal association is incomplete")
	}
	if ownerNS.Valid {
		o.ExecutionPrincipal = serviceAccountIdentityFor(Account{Namespace: ownerNS.String, Name: ownerName.String}, "")
		if ownerUID.Valid {
			o.ExecutionPrincipal.Uid = ownerUID.String
		}
	}
	if creatorNS.Valid {
		o.Creator = serviceAccountIdentityFor(Account{Namespace: creatorNS.String, Name: creatorName.String}, creatorUID.String)
	}
	effectiveCaller, err := scanIdentity(effectiveNS, effectiveName, effectiveUID, "effective caller")
	if err != nil {
		return nil, err
	}
	if callerNS.Valid && callerName.Valid {
		o.WakeCaller = &Account{callerNS.String, callerName.String}
	}
	account, err := scanAdmissionAttribution(o.Creator, creatorPodName, creatorPodUID, effectiveCaller, actionRequestID, actionExecutionID, o.WakeCaller)
	if err != nil {
		return nil, err
	}
	o.Attribution, err = scanAttribution(account, creatorIdentityName, creatorIdentityUID, creatorIdentityPodName, creatorIdentityPodUID)
	if err != nil {
		return nil, err
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
	o.Placement = placementOf(o.Spec)
	if o.Placement == "" {
		return nil, errors.New("stored Session has no placement")
	}
	target := requestTarget(&dispatcherpb.CreateSessionRequest{Spec: o.Spec})
	if target.GetName() != o.SandboxName || target.GetUid() != o.SandboxUID {
		return nil, errors.New("stored Session placement disagrees with executor projection")
	}
	return o, nil
}

// CreateSessionForCaller stores the immutable execution account separately from the verified
// caller attribution. Retrying a concurrently inserted session requires retained read authority.
func (s *Store) CreateSessionWithPlacementForCaller(ctx context.Context, sessionID, sandboxName, sandboxUID string, placement SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account, execution SessionExecution, caller Caller) (*Session, error) {
	executionPrincipal := execution.Principal
	// An Identity-run Session records no ServiceAccount; every other names one.
	if executionPrincipal == nil && execution.Identity == nil || executionPrincipal != nil &&
		(executionPrincipal.GetAccount().GetNamespace() == "" || executionPrincipal.GetAccount().GetName() == "") ||
		callerAttribution(caller) == nil {
		return nil, errors.New("execution principal and creator identity are required")
	}
	if account, ok := caller.(ServiceAccountCaller); ok && (account.Attribution.Actor == nil ||
		account.Attribution.Actor.GetAccount().GetNamespace() == "" || account.Attribution.Actor.GetAccount().GetName() == "" ||
		account.Attribution.Actor.GetUid() == "") {
		return nil, errors.New("execution principal and creator identity are required")
	}
	if err := validateCaller(caller, wakeCaller); err != nil {
		return nil, err
	}
	if identity := execution.Identity; identity != nil && (identity.GetName() == "" || identity.GetUid() == "") {
		return nil, errors.New("execution identity requires a name and UID")
	}
	return s.openSession(ctx, sessionID, sandboxName, sandboxUID, placement, spec, setupScript, wakeCaller, execution, caller)
}

// authorizeRetry uses retained read authority: a receipt retry performs no new execution.
func (o *Session) authorizeRetry(caller Caller) error {
	switch c := caller.(type) {
	case ServiceAccountCaller:
		if c.Privileged || o.ExecutionPrincipal != nil && o.ExecutionPrincipal.GetUid() != "" &&
			sameServiceAccountIdentity(o.ExecutionPrincipal, c.ServiceAccountIdentity) {
			return nil
		}
	case IdentityCaller:
		if o.ExecutionIdentity != nil && sameIdentity(o.ExecutionIdentity, c.Identity) {
			return nil
		}
	}
	return ErrNotFound
}

func (o *Session) matchRequest(sandboxName, sandboxUID string, placement SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string) error {
	if spec == nil {
		return ErrSessionConflict
	}
	oldBytes, err := wire(o.Spec)
	if err != nil {
		return err
	}
	data, err := wire(spec)
	if err != nil {
		return err
	}
	if o.SandboxName != sandboxName || o.SandboxUID != sandboxUID || o.Placement != placement || !bytes.Equal(oldBytes, data) || !ptr.Equal(o.SetupScript, setupScript) {
		return ErrSessionConflict
	}
	return nil
}

func (s *Store) openSession(ctx context.Context, sessionID, sandboxName, sandboxUID string, placement SessionPlacement, spec *sessionpb.SessionSpec, setupScript *string, wakeCaller *Account, execution SessionExecution, caller Caller) (*Session, error) {
	executionPrincipal := execution.Principal
	if sessionID == "" || sandboxName == "" || spec == nil {
		return nil, errors.New("session_id, sandbox and spec are required")
	}
	if !placement.matchesSpec(spec) {
		return nil, errors.New("Session placement does not match spec")
	}
	target := requestTarget(&dispatcherpb.CreateSessionRequest{Spec: spec})
	if target.GetName() != sandboxName || target.GetUid() != sandboxUID {
		return nil, errors.New("Session placement does not match executor projection")
	}
	data, err := wire(spec)
	if err != nil {
		return nil, err
	}
	wakeAssessment, err := wire(initialWakeAssessment())
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
			if caller != nil {
				if err := existing.authorizeRetry(caller); err != nil {
					return err
				}
			}
			if err := existing.matchRequest(sandboxName, sandboxUID, placement, spec, setupScript); err != nil {
				return err
			}
			result = existing
			return nil
		}
		if !errors.Is(err, sql.ErrNoRows) {
			return err
		}
		var ns, name any
		if wakeCaller != nil {
			ns, name = wakeCaller.Namespace, wakeCaller.Name
		}
		var ownerNS, ownerName, ownerUID, creatorNS, creatorName, creatorUID, identityName, identityUID any
		if execution.Identity != nil {
			identityName, identityUID = execution.Identity.GetName(), execution.Identity.GetUid()
		}
		if executionPrincipal != nil {
			ownerNS, ownerName = executionPrincipal.GetAccount().GetNamespace(), executionPrincipal.GetAccount().GetName()
			if executionPrincipal.GetUid() != "" {
				ownerUID = executionPrincipal.GetUid()
			}
		}
		attribution := callerAttribution(caller)
		if account, ok := attribution.(*AdmissionAttribution); ok {
			creator := account.Actor
			creatorNS, creatorName, creatorUID = creator.GetAccount().GetNamespace(), creator.GetAccount().GetName(), creator.GetUid()
		}
		attributionArgs := attributionValues(attribution)
		identityArgs := identityAttributionValues(attribution)
		result, err = scanSession(tx.QueryRowContext(ctx, `INSERT INTO session
			(session_id,sandbox_name,sandbox_uid,execution_account_namespace,execution_account_name,execution_account_uid,
			 creator_account_namespace,creator_account_name,creator_account_uid,effective_caller_account_namespace,effective_caller_account_name,effective_caller_account_uid,
			 creator_pod_name,creator_pod_uid,action_request_id,action_execution_id,wake_eligible,wake_caller_namespace,wake_caller_name,
			 spec,setup_script,state,traceparent,created_at,updated_at,delivery_blocker,wake_assessment,
			 execution_identity_name,execution_identity_uid,
			 creator_identity_name,creator_identity_uid,creator_identity_pod_name,creator_identity_pod_uid)
			VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,now(),now(),$24,$25,$26,$27,$28,$29,$30,$31) RETURNING `+sessionColumns,
			sessionID, sandboxName, sandboxUID, ownerNS, ownerName, ownerUID, creatorNS, creatorName, creatorUID,
			attributionArgs[5], attributionArgs[6], attributionArgs[7], attributionArgs[3], attributionArgs[4], attributionArgs[8], attributionArgs[9],
			wakeCaller != nil, ns, name, data, setupScript, SessionWaitingState,
			observability.Traceparent(ctx), nil, wakeAssessment, identityName, identityUID,
			identityArgs[0], identityArgs[1], identityArgs[2], identityArgs[3]))
		if err != nil {
			return err
		}
		return notify(ctx, tx)
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
	scope, arguments := ownerScope(caller, "", 2)
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
	scope, arguments := ownerScope(caller, "", 3)
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

// BindExecutionPrincipal fixes the UID of a pending managed account exactly once. The expected
// account name and Sandbox UID are part of the predicate, so reconciliation cannot adopt another
// Sandbox's account or overwrite a prior binding.
func (s *Store) BindExecutionPrincipal(ctx context.Context, sessionID, sandboxUID, accountNamespace, accountName, accountUID string) (*Session, error) {
	if sessionID == "" || sandboxUID == "" || accountNamespace == "" || accountName == "" || accountUID == "" {
		return nil, errors.New("Session, Sandbox and ServiceAccount identities are required")
	}
	var result *Session
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		result, err = scanSession(tx.QueryRowContext(ctx, `UPDATE session SET execution_account_uid=$5,updated_at=now()
			WHERE session_id=$1 AND sandbox_uid=$2 AND execution_account_namespace=$3 AND execution_account_name=$4
			AND execution_account_uid IS NULL RETURNING `+sessionColumns,
			sessionID, sandboxUID, accountNamespace, accountName, accountUID))
		if errors.Is(err, sql.ErrNoRows) {
			result, err = scanSession(tx.QueryRowContext(ctx, `SELECT `+sessionColumns+` FROM session
				WHERE session_id=$1 AND sandbox_uid=$2 AND execution_account_namespace=$3 AND execution_account_name=$4
				AND execution_account_uid=$5`, sessionID, sandboxUID, accountNamespace, accountName, accountUID))
			if errors.Is(err, sql.ErrNoRows) {
				return ErrNotAuthorized
			}
			return err
		}
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return result, err
}

func (s *Store) SessionHeads(ctx context.Context) ([]*Session, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT "+sessionColumns+" FROM session WHERE state=$1 ORDER BY created_at", SessionWaitingState)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []*Session
	for rows.Next() {
		o, err := scanSession(rows)
		if err != nil {
			return nil, err
		}
		result = append(result, o)
	}
	return result, rows.Err()
}

// SessionHead reads the durable head after a queue key becomes runnable. The
// in-process queue never decides ordering or whether an session is complete.
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

func (s *Store) Heads(ctx context.Context) ([]*Dispatch, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT `+dispatchColumns+` FROM dispatch d WHERE d.state=$1
		AND NOT EXISTS (SELECT 1 FROM dispatch earlier WHERE earlier.session_id=d.session_id
			AND earlier.position<d.position AND earlier.state IN ($1,$2))
		AND NOT EXISTS (SELECT 1 FROM session o WHERE o.session_id=d.session_id
			AND o.state IN ($3,$4)) ORDER BY d.position`, QueueState, DeliveringState, SessionWaitingState, SessionInProgressState)
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

// SandboxesWithPendingWork lists the Sandboxes owning a QUEUED or DELIVERING
// dispatch or session, the only rows an owner changes. Unlike Heads, it counts
// a DELIVERING row and the QUEUED rows blocked behind it, which a successor
// must requeue after the previous owner dies.
func (s *Store) SandboxesWithPendingWork(ctx context.Context) (map[string]struct{}, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT sandbox_uid::text FROM dispatch WHERE state = ANY($1)
		UNION SELECT sandbox_uid::text FROM session WHERE state = ANY($2)`,
		pq.Array([]string{QueueState, DeliveringState}), pq.Array([]string{SessionWaitingState, SessionInProgressState}))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	result := map[string]struct{}{}
	for rows.Next() {
		var uid string
		if err := rows.Scan(&uid); err != nil {
			return nil, err
		}
		result[uid] = struct{}{}
	}
	return result, rows.Err()
}

func (s *Store) OwnershipGeneration(ctx context.Context, sandboxUID string) (int64, error) {
	var generation int64
	err := s.DB.QueryRowContext(ctx, "SELECT generation FROM sandbox_ownership WHERE sandbox_uid=$1", sandboxUID).Scan(&generation)
	if errors.Is(err, sql.ErrNoRows) {
		return 0, nil
	}
	return generation, err
}

func ownerMatch(ctx context.Context, tx *sql.Tx, owner Owner) (bool, error) {
	var generation int64
	var leaseUID, holder string
	var transitions int
	err := tx.QueryRowContext(ctx, `SELECT generation, lease_uid, lease_transitions, holder_identity
		FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, owner.SandboxUID).Scan(&generation, &leaseUID, &transitions, &holder)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return generation == owner.Generation && leaseUID == owner.LeaseUID && transitions == owner.LeaseTransitions && holder == owner.HolderIdentity, nil
}

func (s *Store) IsCurrentOwner(ctx context.Context, owner Owner) (bool, error) {
	var generation int64
	var leaseUID, holder string
	var transitions int
	err := s.DB.QueryRowContext(ctx, `SELECT generation, lease_uid, lease_transitions, holder_identity
		FROM sandbox_ownership WHERE sandbox_uid=$1`, owner.SandboxUID).Scan(&generation, &leaseUID, &transitions, &holder)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return generation == owner.Generation && leaseUID == owner.LeaseUID && transitions == owner.LeaseTransitions && holder == owner.HolderIdentity, nil
}

// ClaimSandboxOwner must be called only after validating a fresh Kubernetes
// Lease. expectedGeneration is read before Lease acquisition; this CAS prevents
// a paused old holder from replacing a successor's DB fence.
func (s *Store) ClaimSandboxOwner(ctx context.Context, sandboxUID, leaseUID string, leaseTransitions int, holder string, expectedGeneration int64) (*Owner, error) {
	var result *Owner
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		if err := lockLiveSandbox(ctx, tx, sandboxUID); err != nil {
			return err
		}
		var generation int64
		var oldLease, oldHolder string
		var oldTransitions int
		err := tx.QueryRowContext(ctx, `SELECT generation,lease_uid,lease_transitions,holder_identity
			FROM sandbox_ownership WHERE sandbox_uid=$1 FOR UPDATE`, sandboxUID).Scan(&generation, &oldLease, &oldTransitions, &oldHolder)
		if errors.Is(err, sql.ErrNoRows) {
			generation = 0
		} else if err != nil {
			return err
		}
		if generation != expectedGeneration {
			return nil
		}
		same := generation != 0 && oldLease == leaseUID && oldTransitions == leaseTransitions && oldHolder == holder
		if generation == 0 {
			generation = 1
			err = tx.QueryRowContext(ctx, `INSERT INTO sandbox_ownership
				(sandbox_uid,generation,lease_uid,lease_transitions,holder_identity,updated_at)
				VALUES ($1,$2,$3,$4,$5,now()) ON CONFLICT DO NOTHING RETURNING generation`,
				sandboxUID, generation, leaseUID, leaseTransitions, holder).Scan(&generation)
			if errors.Is(err, sql.ErrNoRows) {
				return nil
			}
		} else if !same {
			if oldLease == leaseUID && leaseTransitions <= oldTransitions {
				return nil
			}
			generation++
			_, err = tx.ExecContext(ctx, `UPDATE sandbox_ownership SET generation=$2,lease_uid=$3,
				lease_transitions=$4,holder_identity=$5,updated_at=now() WHERE sandbox_uid=$1`,
				sandboxUID, generation, leaseUID, leaseTransitions, holder)
		}
		if err != nil {
			return err
		}
		if !same {
			if err := notify(ctx, tx); err != nil {
				return err
			}
		}
		result = &Owner{sandboxUID, leaseUID, holder, generation, leaseTransitions}
		return nil
	})
	return result, err
}

// MarkDeliveryAttempt commits possible forwarding before issuing the external RPC.
// Requeue, restart and lease handoff must never turn it back into known-unsent work.
func (s *Store) MarkDeliveryAttempt(ctx context.Context, id string, owner Owner) (bool, error) {
	marked := false
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE dispatch SET delivery_attempted=true
			WHERE command_id=$1 AND sandbox_uid=$2 AND state=$3`, id, owner.SandboxUID, DeliveringState)
		if err != nil {
			return err
		}
		count, err := result.RowsAffected()
		marked = count == 1
		return err
	})
	return marked, err
}

func (s *Store) Settle(ctx context.Context, id string, owner Owner, from []string, to, reason string, admission *eventlogpb.EventEntry) (*Dispatch, error) {
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
			payload, err = wire(admission)
			if err != nil {
				return err
			}
		}
		result, err = scanDispatch(tx.QueryRowContext(ctx, `UPDATE dispatch SET state=CASE WHEN $3='FAILED' AND state='QUEUED_IN_DISPATCHER' AND delivery_attempted THEN 'UNKNOWN' ELSE $3 END, reason=$4, admission=$5, updated_at=now()
			WHERE command_id=$1 AND sandbox_uid=$2 AND state = ANY($6) RETURNING `+dispatchColumns,
			id, owner.SandboxUID, to, nullIfEmpty(reason), payload, pqStringArray(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if result != nil {
			return notify(ctx, tx)
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
func pqStringArray(values []string) any { return pq.Array(values) }

func (s *Store) SettleSession(ctx context.Context, id string, owner Owner, from []string, to, reason string) (*Session, error) {
	return s.settleSession(ctx, id, owner, from, to, reason, false)
}

// RejectSession records a proven native rejection and fails only commands that have
// not left the queue. An older FAILED row alone cannot supply this evidence.
func (s *Store) RejectSession(ctx context.Context, id string, owner Owner, from []string, reason string) (*Session, error) {
	return s.settleSession(ctx, id, owner, from, SessionFailedState, reason, true)
}

func (s *Store) settleSession(ctx context.Context, id string, owner Owner, from []string, to, reason string, rejectQueued bool) (*Session, error) {
	var result *Session
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		// Enqueue holds the same Session lock, so it either precedes rejection and
		// is failed below, or observes the terminal session before storing work.
		if err := sessionLock(ctx, tx, id); err != nil {
			return err
		}
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err = scanSession(tx.QueryRowContext(ctx, `UPDATE session SET state=$3,reason=$4,updated_at=now()
			WHERE session_id=$1 AND sandbox_uid=$2 AND state = ANY($5) RETURNING `+sessionColumns,
			id, owner.SandboxUID, to, nullIfEmpty(reason), pqStringArray(from)))
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		if rejectQueued {
			if _, err := tx.ExecContext(ctx, `UPDATE dispatch SET state=CASE WHEN delivery_attempted THEN 'UNKNOWN' ELSE $3 END,reason='session_rejected',updated_at=now()
				WHERE session_id=$1 AND sandbox_uid=$2 AND state=$4`, id, owner.SandboxUID, FailedState, QueueState); err != nil {
				return err
			}
		}
		return notify(ctx, tx)
	})
	return result, err
}

// RecoverSandbox requeues uncertain in-flight work after taking the successor's
// owner fence. The runner's command/session IDs make replay safe after a lost reply.
func (s *Store) RecoverSandbox(ctx context.Context, owner Owner) (bool, error) {
	current := false
	err := transaction(ctx, s.DB, func(tx *sql.Tx) error {
		var err error
		current, err = ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE dispatch SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE sandbox_uid=$1 AND state=$3`, owner.SandboxUID, QueueState, DeliveringState)
		if err != nil {
			return err
		}
		_, err = tx.ExecContext(ctx, `UPDATE session SET state=$2, reason='dispatcher_restarted', updated_at=now()
			WHERE sandbox_uid=$1 AND state=$3`, owner.SandboxUID, SessionWaitingState, SessionInProgressState)
		if err != nil {
			return err
		}
		return notify(ctx, tx)
	})
	return current, err
}

// NoteWaiting changes only a still-queued row under the current Sandbox fence.
func (s *Store) NoteWaiting(ctx context.Context, id string, owner Owner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE dispatch SET reason=$3,updated_at=now()
			WHERE command_id=$1 AND sandbox_uid=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SandboxUID, reason, QueueState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx)
		}
		return nil
	})
}

func (s *Store) NoteSessionWaiting(ctx context.Context, id string, owner Owner, reason string) error {
	return transaction(ctx, s.DB, func(tx *sql.Tx) error {
		current, err := ownerMatch(ctx, tx, owner)
		if err != nil || !current {
			return err
		}
		result, err := tx.ExecContext(ctx, `UPDATE session SET reason=$3,updated_at=now()
			WHERE session_id=$1 AND sandbox_uid=$2 AND state=$4 AND reason IS DISTINCT FROM $3`,
			id, owner.SandboxUID, reason, SessionWaitingState)
		if err != nil {
			return err
		}
		n, err := result.RowsAffected()
		if err != nil {
			return err
		}
		if n > 0 {
			return notify(ctx, tx)
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
	owner Owner,
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
	assessmentBytes, err := wire(assessment)
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
			WHERE `+key+`=$1 AND sandbox_uid=$2 AND state=$5
			AND (delivery_blocker IS DISTINCT FROM $3 OR wake_assessment IS DISTINCT FROM $4)`,
			id, owner.SandboxUID, blockerBytes, assessmentBytes, waiting)
		if err != nil {
			return err
		}
		changed, err := result.RowsAffected()
		if err != nil || changed == 0 {
			return err
		}
		return notify(ctx, tx)
	})
}
