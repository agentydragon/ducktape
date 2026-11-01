package mcpserver

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/structpb"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// The views below are what the tools answer: the core's records under the tool schemas' snake_case names and
// lower-case enum values. Free-form values (arguments, schemas, errors) stay structpb values, which marshal
// themselves.

var defaultReceiptFields = []string{"id", "state", "version", "created_at", "updated_at"}

// shown is a field include_fields selects: one left unselected is omitted, and one selected but absent reads null.
type shown[T any] struct {
	value T
	ok    bool
}

func show[T any](value T) shown[T] { return shown[T]{value: value, ok: true} }

func (s shown[T]) IsZero() bool { return !s.ok }

func (s shown[T]) MarshalJSON() ([]byte, error) { return json.Marshal(s.value) }

type receipt struct {
	ID        shown[uuid.UUID]      `json:"id,omitzero"`
	State     shown[string]         `json:"state,omitzero"`
	Version   shown[int64]          `json:"version,omitzero"`
	CreatedAt shown[time.Time]      `json:"created_at,omitzero"`
	UpdatedAt shown[time.Time]      `json:"updated_at,omitzero"`
	Input     shown[requestInput]   `json:"input,omitzero"`
	Caller    shown[serviceAccount] `json:"caller,omitzero"`
	CallerUID shown[*string]        `json:"caller_uid,omitzero"`
	Decision  shown[*decision]      `json:"decision,omitzero"`
	Execution shown[*execution]     `json:"execution,omitzero"`
}

type requestInput struct {
	IdempotencyKey string           `json:"idempotency_key"`
	Action         actionIdentity   `json:"action"`
	Arguments      *structpb.Struct `json:"arguments"`
	Title          string           `json:"title"`
	Description    *string          `json:"description"`
}

type serviceAccount struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
}

type operator struct {
	Issuer  string `json:"issuer"`
	Subject string `json:"subject"`
}

type decision struct {
	ID                uuid.UUID       `json:"id"`
	Verdict           string          `json:"verdict"`
	Provider          string          `json:"provider"`
	Operator          *operator       `json:"operator"`
	DecisionNote      *string         `json:"decision_note"`
	ReasonCode        *string         `json:"reason_code"`
	ReasonDescription *string         `json:"reason_description"`
	PolicyEvidence    *policyEvidence `json:"policy_evidence"`
	IdempotencyKey    string          `json:"idempotency_key"`
	DecidedAt         time.Time       `json:"decided_at"`
}

type policyEvidence struct {
	Bindings   []bindingEvidence   `json:"bindings"`
	PolicySets []policySetEvidence `json:"policy_sets"`
	Matched    matchedPolicy       `json:"matched"`
}

type bindingEvidence struct {
	Namespace       string `json:"namespace"`
	Name            string `json:"name"`
	ResourceVersion string `json:"resource_version"`
}

type policySetEvidence struct {
	Namespace  string `json:"namespace"`
	Name       string `json:"name"`
	Generation int64  `json:"generation"`
}

type matchedPolicy struct {
	Namespace  string             `json:"namespace"`
	PolicySet  string             `json:"policy_set"`
	Source     string             `json:"source"`
	Index      int32              `json:"index"`
	Type       string             `json:"type"`
	Repository *matchedRepository `json:"repository"`
}

type matchedRepository struct {
	Owner           string `json:"owner"`
	Repository      string `json:"repository"`
	ConfirmedPublic bool   `json:"confirmed_public"`
}

// execution is an Execution as a receipt reports it: never its result, which only the tool answer carries.
type execution struct {
	ID           uuid.UUID        `json:"id"`
	State        string           `json:"state"`
	Error        *structpb.Struct `json:"error"`
	CreatedAt    time.Time        `json:"created_at"`
	StartedAt    *time.Time       `json:"started_at"`
	CompletedAt  *time.Time       `json:"completed_at"`
	ReconciledAt *time.Time       `json:"reconciled_at"`
}

type event struct {
	Sequence int32     `json:"sequence"`
	State    string    `json:"state"`
	At       time.Time `json:"at"`
	// Actor is a callerActor or an operator; absent when no one is named.
	Actor any `json:"actor,omitempty"`
}

type callerActor struct {
	Account   serviceAccount `json:"account"`
	CallerUID *string        `json:"caller_uid,omitempty"`
}

type policyView struct {
	Synced        shown[bool]              `json:"synced,omitzero"`
	AutoApproveIf shown[[]effectivePolicy] `json:"auto_approve_if,omitzero"`
	Subject       shown[serviceAccount]    `json:"subject,omitzero"`
	Bindings      shown[[]callerBinding]   `json:"bindings,omitzero"`
}

type effectivePolicy struct {
	Binding   string `json:"binding"`
	PolicySet string `json:"policy_set"`
	Source    string `json:"source"`
	Index     int32  `json:"index"`
	Policy    any    `json:"policy"`
}

type callerBinding struct {
	Name       string     `json:"name"`
	ExpiresAt  *time.Time `json:"expires_at"`
	PolicySets []string   `json:"policy_sets"`
}

// actionLists maps a group to the names of its Actions a policy lists, or an entity to its services.
type actionLists map[string][]string

// actionsRule is a rule that names only Actions: exact_actions and github_public_repository.
type actionsRule struct {
	Type    string      `json:"type"`
	Actions actionLists `json:"actions"`
}

type argumentSchemaRule struct {
	Type           string           `json:"type"`
	Actions        actionLists      `json:"actions"`
	ArgumentSchema *structpb.Struct `json:"argument_schema"`
}

type githubRepositoryRule struct {
	Type       string      `json:"type"`
	Actions    actionLists `json:"actions"`
	Owner      string      `json:"owner"`
	Repository string      `json:"repository"`
}

type homeAssistantEntityControlRule struct {
	Type     string      `json:"type"`
	Actions  actionLists `json:"actions"`
	Entities actionLists `json:"entities"`
}

// viewDecoder turns core answers into views, keeping the first answer it could not read: an enum outside the
// contract, an unparsable id or a missing timestamp fails the call.
type viewDecoder struct {
	err error
}

func (d *viewDecoder) fail(format string, args ...any) {
	if d.err == nil {
		d.err = fmt.Errorf(format, args...)
	}
}

type protoEnum interface {
	~int32
	String() string
}

// enum is a proto enum value as the tool schemas spell it: the name without its prefix, in lower case.
func enum[E protoEnum](d *viewDecoder, value E, prefix string) string {
	name, ok := strings.CutPrefix(value.String(), prefix)
	if value == 0 || !ok {
		d.fail("core answered %s%v, outside the contract", prefix, value)
		return ""
	}
	return strings.ToLower(name)
}

func (d *viewDecoder) uuid(text string) uuid.UUID {
	id, err := uuid.Parse(text)
	if err != nil {
		d.fail("core answered id %q: %w", text, err)
	}
	return id
}

func (d *viewDecoder) time(at *timestamppb.Timestamp) time.Time {
	if at == nil {
		d.fail("core answered without a required timestamp")
		return time.Time{}
	}
	return at.AsTime()
}

func (d *viewDecoder) optionalTime(at *timestamppb.Timestamp) *time.Time {
	if at == nil {
		return nil
	}
	value := d.time(at)
	return &value
}

func accountView(account *sandboxes.ServiceAccount) serviceAccount {
	return serviceAccount{Namespace: account.GetNamespace(), Name: account.GetName()}
}

// object is a free-form object the contract always carries, as an empty one when the core left it out.
func object(value *structpb.Struct) *structpb.Struct {
	if value == nil {
		return &structpb.Struct{}
	}
	return value
}

// receiptView is a request's receipt with only the named fields.
func (d *viewDecoder) receiptView(request *pb.ActionRequest, fields []string) receipt {
	var view receipt
	for _, field := range fields {
		switch field {
		case "id":
			view.ID = show(d.uuid(request.GetId()))
		case "state":
			view.State = show(enum(d, request.GetState(), "ACTION_STATE_"))
		case "version":
			view.Version = show(request.GetVersion())
		case "created_at":
			view.CreatedAt = show(d.time(request.GetCreatedAt()))
		case "updated_at":
			view.UpdatedAt = show(d.time(request.GetUpdatedAt()))
		case "input":
			view.Input = show(requestInput{
				IdempotencyKey: request.GetIdempotencyKey(),
				Action:         actionIdentity{Group: request.GetAction().GetGroup(), Name: request.GetAction().GetName()},
				Arguments:      object(request.GetArguments()),
				Title:          request.GetTitle(),
				Description:    request.Description,
			})
		case "caller":
			view.Caller = show(accountView(request.GetCaller()))
		case "caller_uid":
			view.CallerUID = show(request.CallerUid)
		case "decision":
			view.Decision = show(d.decisionView(request.GetDecision()))
		case "execution":
			view.Execution = show(d.executionView(request.GetExecution()))
		default:
			d.fail("receipt field %q", field)
		}
	}
	return view
}

func (d *viewDecoder) decisionView(from *pb.Decision) *decision {
	if from == nil {
		return nil
	}
	var by *operator
	if from.GetOperator() != nil {
		by = &operator{Issuer: from.GetOperator().GetIssuer(), Subject: from.GetOperator().GetSubject()}
	}
	return &decision{
		ID:                d.uuid(from.GetId()),
		Verdict:           enum(d, from.GetVerdict(), "VERDICT_"),
		Provider:          from.GetProvider(),
		Operator:          by,
		DecisionNote:      from.DecisionNote,
		ReasonCode:        from.ReasonCode,
		ReasonDescription: from.ReasonDescription,
		PolicyEvidence:    d.evidenceView(from.GetPolicyEvidence()),
		IdempotencyKey:    from.GetIdempotencyKey(),
		DecidedAt:         d.time(from.GetDecidedAt()),
	}
}

func (d *viewDecoder) evidenceView(from *pb.PolicyEvidence) *policyEvidence {
	if from == nil {
		return nil
	}
	view := &policyEvidence{
		Bindings:   make([]bindingEvidence, 0, len(from.GetBindings())),
		PolicySets: make([]policySetEvidence, 0, len(from.GetPolicySets())),
	}
	for _, binding := range from.GetBindings() {
		view.Bindings = append(view.Bindings, bindingEvidence{
			Namespace:       binding.GetNamespace(),
			Name:            binding.GetName(),
			ResourceVersion: binding.GetResourceVersion(),
		})
	}
	for _, set := range from.GetPolicySets() {
		view.PolicySets = append(view.PolicySets, policySetEvidence{
			Namespace:  set.GetNamespace(),
			Name:       set.GetName(),
			Generation: set.GetGeneration(),
		})
	}
	matched := from.GetMatched()
	view.Matched = matchedPolicy{
		Namespace: matched.GetNamespace(),
		PolicySet: matched.GetPolicySet(),
		Source:    enum(d, matched.GetSource(), "POLICY_SOURCE_"),
		Index:     matched.GetIndex(),
		Type:      enum(d, matched.GetType(), "POLICY_KIND_"),
	}
	if repository := matched.GetRepository(); repository != nil {
		view.Matched.Repository = &matchedRepository{
			Owner:           repository.GetOwner(),
			Repository:      repository.GetRepository(),
			ConfirmedPublic: repository.GetConfirmedPublic(),
		}
	}
	return view
}

func (d *viewDecoder) executionView(from *pb.Execution) *execution {
	if from == nil {
		return nil
	}
	return &execution{
		ID:           d.uuid(from.GetId()),
		State:        enum(d, from.GetState(), "EXECUTION_STATE_"),
		Error:        from.GetError(),
		CreatedAt:    d.time(from.GetCreatedAt()),
		StartedAt:    d.optionalTime(from.GetStartedAt()),
		CompletedAt:  d.optionalTime(from.GetCompletedAt()),
		ReconciledAt: d.optionalTime(from.GetReconciledAt()),
	}
}

func (d *viewDecoder) eventView(from *pb.ActionEvent) event {
	view := event{
		Sequence: from.GetSequence(),
		State:    enum(d, from.GetState(), "ACTION_STATE_"),
		At:       d.time(from.GetAt()),
	}
	switch actor := from.GetActor().GetPrincipal().(type) {
	case nil:
	case *pb.Principal_Caller:
		view.Actor = callerActor{Account: accountView(actor.Caller), CallerUID: from.GetActor().CallerUid}
	case *pb.Principal_Operator:
		view.Actor = operator{Issuer: actor.Operator.GetIssuer(), Subject: actor.Operator.GetSubject()}
	default:
		d.fail("core answered actor %T, outside the contract", actor)
	}
	return view
}

func (d *viewDecoder) policyView(policy *pb.CallerActionPolicy, fields []string) policyView {
	var view policyView
	for _, field := range fields {
		switch field {
		case "synced":
			view.Synced = show(policy.GetSynced())
		case "auto_approve_if":
			effective := make([]effectivePolicy, 0, len(policy.GetAutoApproveIf()))
			for _, entry := range policy.GetAutoApproveIf() {
				effective = append(effective, effectivePolicy{
					Binding:   entry.GetBinding(),
					PolicySet: entry.GetPolicySet(),
					Source:    enum(d, entry.GetSource(), "POLICY_SOURCE_"),
					Index:     entry.GetIndex(),
					Policy:    d.ruleView(entry.GetPolicy()),
				})
			}
			view.AutoApproveIf = show(effective)
		case "subject":
			view.Subject = show(accountView(policy.GetSubject()))
		case "bindings":
			bindings := make([]callerBinding, 0, len(policy.GetBindings()))
			for _, binding := range policy.GetBindings() {
				// A caller's view of a binding is its name, expiry and the names of its sets.
				sets := make([]string, 0, len(binding.GetPolicySets()))
				for _, set := range binding.GetPolicySets() {
					sets = append(sets, set.GetName())
				}
				bindings = append(bindings, callerBinding{
					Name:       binding.GetName(),
					ExpiresAt:  d.optionalTime(binding.GetExpiresAt()),
					PolicySets: sets,
				})
			}
			view.Bindings = show(bindings)
		default:
			d.fail("policy field %q", field)
		}
	}
	return view
}

func (d *viewDecoder) ruleView(policy *pb.Policy) any {
	switch kind := policy.GetKind().(type) {
	case *pb.Policy_ExactActions:
		return actionsRule{Type: "exact_actions", Actions: sortedLists(kind.ExactActions.GetActions())}
	case *pb.Policy_ArgumentSchema:
		return argumentSchemaRule{
			Type:           "argument_schema",
			Actions:        sortedLists(kind.ArgumentSchema.GetActions()),
			ArgumentSchema: object(kind.ArgumentSchema.GetArgumentSchema()),
		}
	case *pb.Policy_GithubRepository:
		return githubRepositoryRule{
			Type:       "github_repository",
			Actions:    sortedLists(kind.GithubRepository.GetActions()),
			Owner:      kind.GithubRepository.GetOwner(),
			Repository: kind.GithubRepository.GetRepository(),
		}
	case *pb.Policy_GithubPublicRepository:
		return actionsRule{Type: "github_public_repository", Actions: sortedLists(kind.GithubPublicRepository.GetActions())}
	case *pb.Policy_HomeAssistantEntityControl:
		return homeAssistantEntityControlRule{
			Type:     "home_assistant_entity_control",
			Actions:  sortedLists(kind.HomeAssistantEntityControl.GetActions()),
			Entities: sortedLists(kind.HomeAssistantEntityControl.GetEntities()),
		}
	default:
		d.fail("core answered policy %T, outside the contract", kind)
		return nil
	}
}

// sortedLists is a policy's name lists, each sorted.
func sortedLists(lists map[string]*pb.StringList) actionLists {
	view := actionLists{}
	for key, list := range lists {
		view[key] = slices.Sorted(slices.Values(list.GetValues()))
	}
	return view
}
