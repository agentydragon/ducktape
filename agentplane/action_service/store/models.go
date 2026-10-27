package store

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"time"
	"unicode/utf8"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/google/uuid"
)

// ActionIdentity names an Action by its group and name. It is stored as `action_request.action`.
type ActionIdentity struct {
	Group string `json:"group"`
	Name  string `json:"name"`
}

// ReadPrincipal is who reads requests: a CallerPrincipal reads its own, an OperatorPrincipal and a
// ServiceReaderPrincipal read every owner's.
type ReadPrincipal interface{ isReadPrincipal() }

// Principal is an authenticated caller or operator. A shared action.Actor records its identity on an event.
type Principal interface {
	ReadPrincipal
	isPrincipal()
}

// CallerPrincipal is a workload that proved one ServiceAccount.
type CallerPrincipal struct {
	Account action.ServiceAccount `json:"account"`
}

// OperatorPrincipal is the human behind the operator surface, as its issuer verified them.
type OperatorPrincipal struct {
	Issuer  string `json:"issuer"`
	Subject string `json:"subject"`
}

// ServiceReaderPrincipal is an allowlisted workload that reads every owner's requests and changes none.
type ServiceReaderPrincipal struct {
	Account action.ServiceAccount
}

func (CallerPrincipal) isReadPrincipal()        {}
func (OperatorPrincipal) isReadPrincipal()      {}
func (ServiceReaderPrincipal) isReadPrincipal() {}
func (CallerPrincipal) isPrincipal()            {}
func (OperatorPrincipal) isPrincipal()          {}

// ExecutionState is where an Execution is. It is stored as text.
type ExecutionState string

const (
	ExecutionPendingDispatch ExecutionState = "pending_dispatch"
	ExecutionDispatching     ExecutionState = "dispatching"
	ExecutionRunning         ExecutionState = "running"
	ExecutionSucceeded       ExecutionState = "succeeded"
	ExecutionFailed          ExecutionState = "failed"
	ExecutionCancelled       ExecutionState = "cancelled"
	ExecutionUnknown         ExecutionState = "execution_unknown"
)

var executionStates = []ExecutionState{
	ExecutionPendingDispatch, ExecutionDispatching, ExecutionRunning, ExecutionSucceeded, ExecutionFailed,
	ExecutionCancelled, ExecutionUnknown,
}

// Verdict is what a Decision says. It is stored as text.
type Verdict string

const (
	VerdictAllow Verdict = "allow"
	VerdictDeny  Verdict = "deny"
)

// PolicyKind is the type of a policy a Decision matched.
type PolicyKind string

const (
	PolicyExactActions               PolicyKind = "exact_actions"
	PolicyArgumentSchema             PolicyKind = "argument_schema"
	PolicyGitHubRepository           PolicyKind = "github_repository"
	PolicyGitHubPublicRepository     PolicyKind = "github_public_repository"
	PolicyHomeAssistantEntityControl PolicyKind = "home_assistant_entity_control"
)

var policyKinds = []PolicyKind{
	PolicyExactActions, PolicyArgumentSchema, PolicyGitHubRepository, PolicyGitHubPublicRepository,
	PolicyHomeAssistantEntityControl,
}

// PolicySourceRules is the only list of a PolicySet a matched policy sits in.
const PolicySourceRules = "rules"

// PolicyEvidence is what a policy-set Decision evaluated, as the objects stood at admission. It is stored as
// `action_decision.policy_evidence`.
type PolicyEvidence struct {
	Bindings   []BindingEvidence   `json:"bindings"`
	PolicySets []PolicySetEvidence `json:"policy_sets"`
	Matched    MatchedPolicy       `json:"matched"`
}

type BindingEvidence struct {
	Namespace       string `json:"namespace"`
	Name            string `json:"name"`
	ResourceVersion string `json:"resource_version"`
}

type PolicySetEvidence struct {
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	Generation int64  `json:"generation"`
}

// MatchedPolicy is the leaf policy that produced the Decision.
type MatchedPolicy struct {
	Namespace string     `json:"namespace"`
	PolicySet string     `json:"policy_set"`
	Source    string     `json:"source"`
	Index     int64      `json:"index"`
	Type      PolicyKind `json:"type"`
	// Repository is set by the GitHub policy kinds.
	Repository *MatchedRepository `json:"repository"`
}

type MatchedRepository struct {
	Owner      string `json:"owner"`
	Repository string `json:"repository"`
	// ConfirmedPublic is whether an unauthenticated GitHub lookup confirmed the repository public at admission.
	ConfirmedPublic bool `json:"confirmed_public"`
}

// ActionRequestInput is the envelope a caller submits.
type ActionRequestInput struct {
	IdempotencyKey string
	Action         ActionIdentity
	// Arguments is a JSON object.
	Arguments   json.RawMessage
	Title       string
	Description *string
}

// ProviderVote is the decisive outcome of admission's provider evaluation and the provider that cast it.
type ProviderVote struct {
	Provider          string
	Verdict           Verdict
	ReasonCode        string
	ReasonDescription *string
	// Evidence is set when the provider decided from policy objects.
	Evidence *PolicyEvidence
}

// Admission is what admission settled about a submission before the store sees it.
type Admission struct {
	// RequestID is the ID the request is stored under.
	RequestID uuid.UUID
	// Vote commits a Decision with the insert, so the request is never visible as pending. Nil leaves the
	// Decision to a human.
	Vote *ProviderVote
}

// DecisionInput is a human Decision on a pending request.
type DecisionInput struct {
	Verdict Verdict
	// ExpectedVersion is the version of the request the operator decided on.
	ExpectedVersion int64
	IdempotencyKey  string
	DecisionNote    *string
}

// ActionRequest is a request as it is read back. Every reader sees the same one.
type ActionRequest struct {
	ID             uuid.UUID
	IdempotencyKey string
	Action         ActionIdentity
	// Arguments is a JSON object, as submitted.
	Arguments   json.RawMessage
	Title       string
	Description *string
	// Caller is the ServiceAccount that submitted the request.
	Caller action.ServiceAccount
	State  action.State
	// Version counts the request's transitions from 1; an Event's sequence is the version it records.
	Version   int64
	CreatedAt time.Time
	UpdatedAt time.Time
	Decision  *Decision
	Execution *Execution
}

// Decision is the one verdict on a request.
type Decision struct {
	ID       uuid.UUID
	Verdict  Verdict
	Provider string
	// Operator is the human who decided; nil when a DecisionProvider did.
	Operator          *OperatorPrincipal
	DecisionNote      *string
	ReasonCode        *string
	ReasonDescription *string
	PolicyEvidence    *PolicyEvidence
	IdempotencyKey    string
	DecidedAt         time.Time
}

// Execution is the one attempt to run an allowed request.
type Execution struct {
	ID    uuid.UUID
	State ExecutionState
	// Result is any JSON value; nil when there is none.
	Result json.RawMessage
	// Error is a JSON object; nil when there is none.
	Error        json.RawMessage
	CreatedAt    time.Time
	StartedAt    *time.Time
	CompletedAt  *time.Time
	ReconciledAt *time.Time
}

// Event is one transition of a request.
type Event struct {
	Sequence int64
	State    action.State
	At       time.Time
	// Actor is the cancellation author; nil for every other transition.
	Actor action.Actor
}

// CancellationOutcome is what cancelling a request did.
type CancellationOutcome string

const (
	OutcomeCancelled        CancellationOutcome = "cancelled"
	OutcomeAlreadyCancelled CancellationOutcome = "already_cancelled"
	OutcomeAlreadyFinished  CancellationOutcome = "already_finished"
	OutcomeTooLate          CancellationOutcome = "too_late"
)

type CancellationResult struct {
	Outcome CancellationOutcome
	Request ActionRequest
}

// Lease is the bearer that proves which dispatch attempt owns an Execution: the executor and the token granted
// at claim time.
type Lease struct {
	ExecutorID string
	Token      uuid.UUID
}

// ExecutionClaim is the dispatch attempt ClaimExecution reserved: the only one the Execution gets.
type ExecutionClaim struct {
	RequestID uuid.UUID
	Lease
	// ExpiresAt is when the lease lapses unless HeartbeatExecution renews it.
	ExpiresAt time.Time
}

// ExecutionRequest is the payload handed to the one executor dispatch, as stored.
type ExecutionRequest struct {
	RequestID uuid.UUID
	Action    ActionIdentity
	// Arguments is a JSON object.
	Arguments json.RawMessage
	Caller    action.ServiceAccount
}

// ExecutionOutcome is the terminal outcome an executor delivers.
type ExecutionOutcome struct {
	// State is succeeded, failed, cancelled or execution_unknown.
	State ExecutionState
	// Result is any JSON value; nil or JSON null for none.
	Result json.RawMessage
	// Error is a JSON object; nil or JSON null for none.
	Error json.RawMessage
}

// UnknownOutcomeReason is why an Execution ended without an outcome. It is the `kind` of its error.
type UnknownOutcomeReason string

const (
	// UnknownExecutorLost is a lapsed lease whose executor's own heartbeat has also stopped.
	UnknownExecutorLost UnknownOutcomeReason = "executor_lost"
	// UnknownLeaseExpired is a lapsed lease whose executor is otherwise healthy.
	UnknownLeaseExpired UnknownOutcomeReason = "lease_expired"
	// UnknownAdapterOutcome is an executor that could not tell whether the effect happened.
	UnknownAdapterOutcome UnknownOutcomeReason = "execution_outcome_unknown"
	// UnknownCoordinatorStopped is an Execution its worker stopped while draining.
	UnknownCoordinatorStopped UnknownOutcomeReason = "coordinator_stopped"
)

// terminalActionStates is the state of a request whose Execution ended in the key.
var terminalActionStates = map[ExecutionState]action.State{
	ExecutionSucceeded: action.StateSucceeded,
	ExecutionFailed:    action.StateFailed,
	ExecutionCancelled: action.StateCancelled,
	ExecutionUnknown:   action.StateExecutionUnknown,
}

// unknownOutcomeError is the `action_execution.error` of an Execution whose outcome could not be observed.
func unknownOutcomeError(reason UnknownOutcomeReason) string {
	return `{"kind": "` + string(reason) + `", "message": "dispatch outcome unknown; not replayed"}`
}

var keyPattern = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

// invalid is ErrInvalid with the bound or pattern a value breaks.
func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", ErrInvalid, fmt.Sprintf(format, args...))
}

// bounded checks a string's length in characters, as Pydantic counts them.
func bounded(field, value string, minLength, maxLength int) error {
	if length := utf8.RuneCountInString(value); length < minLength || length > maxLength {
		return invalid("%s must be %d to %d characters, not %d", field, minLength, maxLength, length)
	}
	return nil
}

func validateServiceAccount(a action.ServiceAccount) error {
	if err := a.Validate(); err != nil {
		return invalid("%s", err)
	}
	return nil
}

// Validate checks the group and name have the stored form: lowercase letters, digits, `_` and `-`, starting with a
// letter, 1 to 200 characters.
func (a ActionIdentity) Validate() error {
	if err := validateKey("group", a.Group); err != nil {
		return err
	}
	return validateKey("name", a.Name)
}

func validateKey(field, value string) error {
	if err := bounded(field, value, 1, 200); err != nil {
		return err
	}
	if !keyPattern.MatchString(value) {
		return invalid("%s must match %s", field, keyPattern)
	}
	return nil
}

func (p OperatorPrincipal) validate() error {
	if p.Issuer == "" || p.Subject == "" {
		return invalid("an operator needs an issuer and a subject")
	}
	return nil
}

func (e PolicyEvidence) validate() error {
	if e.Matched.Source != PolicySourceRules {
		return invalid("policy source must be %q", PolicySourceRules)
	}
	if !slices.Contains(policyKinds, e.Matched.Type) {
		return invalid("unknown policy kind %q", e.Matched.Type)
	}
	if e.Matched.Index < 0 {
		return invalid("policy index must not be negative")
	}
	return nil
}

func (s ExecutionState) valid() bool { return slices.Contains(executionStates, s) }
func (v Verdict) valid() bool        { return v == VerdictAllow || v == VerdictDeny }

// isObject reports whether data is a JSON object.
func isObject(data json.RawMessage) bool {
	var object map[string]json.RawMessage
	return json.Unmarshal(data, &object) == nil && object != nil
}

func (i ActionRequestInput) Validate() error {
	if err := bounded("idempotency_key", i.IdempotencyKey, 1, 200); err != nil {
		return err
	}
	if err := bounded("title", i.Title, 1, 60); err != nil {
		return err
	}
	if i.Description != nil {
		if err := bounded("description", *i.Description, 0, 250); err != nil {
			return err
		}
	}
	if !isObject(i.Arguments) {
		return invalid("arguments must be a JSON object")
	}
	return i.Action.Validate()
}

func (i DecisionInput) Validate() error {
	if !i.Verdict.valid() {
		return invalid("unknown verdict %q", i.Verdict)
	}
	if i.ExpectedVersion < 1 {
		return invalid("expected_version must be at least 1")
	}
	if err := bounded("idempotency_key", i.IdempotencyKey, 1, 200); err != nil {
		return err
	}
	if i.DecisionNote != nil {
		return bounded("decision_note", *i.DecisionNote, 0, 2000)
	}
	return nil
}

func (o ExecutionOutcome) Validate() error {
	if _, terminal := terminalActionStates[o.State]; !terminal {
		return invalid("an executor returns a terminal state, not %q", o.State)
	}
	if o.Result != nil && !json.Valid(o.Result) {
		return invalid("result must be a JSON value")
	}
	if o.Error != nil && string(o.Error) != "null" && !isObject(o.Error) {
		return invalid("error must be a JSON object")
	}
	return nil
}

func (v ProviderVote) Validate() error {
	if !v.Verdict.valid() {
		return invalid("a vote that decides is allow or deny, not %q", v.Verdict)
	}
	if err := bounded("reason_code", v.ReasonCode, 1, 64); err != nil {
		return err
	}
	if v.ReasonDescription != nil {
		if err := bounded("reason_description", *v.ReasonDescription, 0, 500); err != nil {
			return err
		}
	}
	if v.Evidence != nil {
		return v.Evidence.validate()
	}
	return nil
}

// decodeFields reads one JSON object into v, refusing fields v does not name and, as the stored models do, a
// missing or null field named in required.
func decodeFields(data []byte, v any, required ...string) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for _, name := range required {
		if value, present := fields[name]; !present || string(value) == "null" {
			return fmt.Errorf("%s is required", name)
		}
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	return decoder.Decode(v)
}

func (a *ActionIdentity) UnmarshalJSON(data []byte) error {
	type plain ActionIdentity
	if err := decodeFields(data, (*plain)(a), "group", "name"); err != nil {
		return err
	}
	return a.Validate()
}

func (e *PolicyEvidence) UnmarshalJSON(data []byte) error {
	type plain PolicyEvidence
	if err := decodeFields(data, (*plain)(e), "bindings", "policy_sets", "matched"); err != nil {
		return err
	}
	return e.validate()
}

func (b *BindingEvidence) UnmarshalJSON(data []byte) error {
	type plain BindingEvidence
	return decodeFields(data, (*plain)(b), "namespace", "name", "resource_version")
}

func (s *PolicySetEvidence) UnmarshalJSON(data []byte) error {
	type plain PolicySetEvidence
	return decodeFields(data, (*plain)(s), "namespace", "name", "generation")
}

func (m *MatchedPolicy) UnmarshalJSON(data []byte) error {
	type plain MatchedPolicy
	return decodeFields(data, (*plain)(m), "namespace", "policy_set", "source", "index", "type")
}

func (r *MatchedRepository) UnmarshalJSON(data []byte) error {
	type plain MatchedRepository
	return decodeFields(data, (*plain)(r), "owner", "repository", "confirmed_public")
}

// MarshalJSON writes the empty lists a nil slice stands for, which the stored model requires.
func (e PolicyEvidence) MarshalJSON() ([]byte, error) {
	type plain PolicyEvidence
	if e.Bindings == nil {
		e.Bindings = []BindingEvidence{}
	}
	if e.PolicySets == nil {
		e.PolicySets = []PolicySetEvidence{}
	}
	return json.Marshal(plain(e))
}
