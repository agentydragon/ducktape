package mcpserver

import (
	"fmt"
	"maps"
	"slices"
	"strings"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// receiptFields are a receipt's top-level fields in the order it lists them; include_fields picks among them.
var receiptFields = []string{
	"id", "state", "version", "created_at", "updated_at", "input", "caller", "decision", "execution",
}

var defaultReceiptFields = receiptFields[:5]

// viewDecoder turns core answers into the views the Python tools answered with, keeping the first answer it
// could not read: an enum outside the contract, an unparsable id or a missing timestamp fails the call.
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

// enum is a proto enum value as Python's StrEnum spells it: the name without its prefix, in lower case.
func enum[E protoEnum](d *viewDecoder, value E, prefix string) string {
	name, ok := strings.CutPrefix(value.String(), prefix)
	if value == 0 || !ok {
		d.fail("core answered %s%v, outside the contract", prefix, value)
		return ""
	}
	return strings.ToLower(name)
}

func (d *viewDecoder) uuid(text string) string {
	id, err := uuid.Parse(text)
	if err != nil {
		d.fail("core answered id %q: %w", text, err)
		return ""
	}
	return id.String()
}

// time is a timestamp as Pydantic writes an aware UTC datetime.
func (d *viewDecoder) time(at *timestamppb.Timestamp) string {
	if at == nil {
		d.fail("core answered without a required timestamp")
		return ""
	}
	return pythonDatetime(at.AsTime())
}

func (d *viewDecoder) optionalTime(at *timestamppb.Timestamp) any {
	if at == nil {
		return nil
	}
	return d.time(at)
}

func pythonDatetime(at time.Time) string {
	at = at.UTC()
	text := at.Format("2006-01-02T15:04:05")
	if micros := at.Nanosecond() / 1000; micros != 0 {
		text += fmt.Sprintf(".%06d", micros)
	}
	return text + "Z"
}

func optionalString(text *string) any {
	if text == nil {
		return nil
	}
	return *text
}

func serviceAccountView(account *sandboxes.ServiceAccount) jsonObject {
	return jsonObject{{"namespace", account.GetNamespace()}, {"name", account.GetName()}}
}

func identityView(identity *pb.ActionIdentity) jsonObject {
	return jsonObject{{"group", identity.GetGroup()}, {"name", identity.GetName()}}
}

// receiptView is the receipt of a request with only the named fields, in the receipt's own field order.
func (d *viewDecoder) receiptView(request *pb.ActionRequest, fields []string) jsonObject {
	view := jsonObject{}
	for _, field := range receiptFields {
		if !slices.Contains(fields, field) {
			continue
		}
		var value any
		switch field {
		case "id":
			value = d.uuid(request.GetId())
		case "state":
			value = enum(d, request.GetState(), "ACTION_STATE_")
		case "version":
			value = request.GetVersion()
		case "created_at":
			value = d.time(request.GetCreatedAt())
		case "updated_at":
			value = d.time(request.GetUpdatedAt())
		case "input":
			value = jsonObject{
				{"idempotency_key", request.GetIdempotencyKey()},
				{"action", identityView(request.GetAction())},
				{"arguments", freeFormObject(request.GetArguments())},
				{"title", request.GetTitle()},
				{"description", optionalString(request.Description)},
			}
		case "caller":
			value = serviceAccountView(request.GetCaller())
		case "decision":
			value = d.decisionView(request.GetDecision())
		case "execution":
			value = d.executionView(request.GetExecution())
		}
		view = append(view, jsonMember{field, value})
	}
	return view
}

func (d *viewDecoder) decisionView(decision *pb.Decision) any {
	if decision == nil {
		return nil
	}
	var operator any
	if decision.GetOperator() != nil {
		operator = jsonObject{{"issuer", decision.GetOperator().GetIssuer()}, {"subject", decision.GetOperator().GetSubject()}}
	}
	return jsonObject{
		{"id", d.uuid(decision.GetId())},
		{"verdict", enum(d, decision.GetVerdict(), "VERDICT_")},
		{"provider", decision.GetProvider()},
		{"operator", operator},
		{"decision_note", optionalString(decision.DecisionNote)},
		{"reason_code", optionalString(decision.ReasonCode)},
		{"reason_description", optionalString(decision.ReasonDescription)},
		{"policy_evidence", d.evidenceView(decision.GetPolicyEvidence())},
		{"idempotency_key", decision.GetIdempotencyKey()},
		{"decided_at", d.time(decision.GetDecidedAt())},
	}
}

func (d *viewDecoder) evidenceView(evidence *pb.PolicyEvidence) any {
	if evidence == nil {
		return nil
	}
	bindings := []any{}
	for _, binding := range evidence.GetBindings() {
		bindings = append(bindings, jsonObject{
			{"namespace", binding.GetNamespace()},
			{"name", binding.GetName()},
			{"resource_version", binding.GetResourceVersion()},
		})
	}
	sets := []any{}
	for _, set := range evidence.GetPolicySets() {
		sets = append(sets, jsonObject{
			{"namespace", set.GetNamespace()},
			{"name", set.GetName()},
			{"generation", set.GetGeneration()},
		})
	}
	matched := evidence.GetMatched()
	var repository any
	if matched.GetRepository() != nil {
		repository = jsonObject{
			{"owner", matched.GetRepository().GetOwner()},
			{"repository", matched.GetRepository().GetRepository()},
			{"confirmed_public", matched.GetRepository().GetConfirmedPublic()},
		}
	}
	return jsonObject{
		{"bindings", bindings},
		{"policy_sets", sets},
		{"matched", jsonObject{
			{"namespace", matched.GetNamespace()},
			{"policy_set", matched.GetPolicySet()},
			{"source", enum(d, matched.GetSource(), "POLICY_SOURCE_")},
			{"index", int64(matched.GetIndex())},
			{"type", enum(d, matched.GetType(), "POLICY_KIND_")},
			{"repository", repository},
		}},
	}
}

// executionView is an Execution as a receipt reports it: never its result, which only the tool answer carries.
func (d *viewDecoder) executionView(execution *pb.Execution) any {
	if execution == nil {
		return nil
	}
	var failure any
	if execution.GetError() != nil {
		failure = freeFormObject(execution.GetError())
	}
	return jsonObject{
		{"id", d.uuid(execution.GetId())},
		{"state", enum(d, execution.GetState(), "EXECUTION_STATE_")},
		{"error", failure},
		{"created_at", d.time(execution.GetCreatedAt())},
		{"started_at", d.optionalTime(execution.GetStartedAt())},
		{"completed_at", d.optionalTime(execution.GetCompletedAt())},
		{"reconciled_at", d.optionalTime(execution.GetReconciledAt())},
	}
}

func (d *viewDecoder) eventView(event *pb.ActionEvent) jsonObject {
	view := jsonObject{
		{"sequence", int64(event.GetSequence())},
		{"state", enum(d, event.GetState(), "ACTION_STATE_")},
		{"at", d.time(event.GetAt())},
	}
	switch actor := event.GetActor().GetPrincipal().(type) {
	case nil:
	case *pb.Principal_Caller:
		view = append(view, jsonMember{"actor", jsonObject{{"account", serviceAccountView(actor.Caller)}}})
	case *pb.Principal_Operator:
		view = append(view, jsonMember{"actor", jsonObject{
			{"issuer", actor.Operator.GetIssuer()},
			{"subject", actor.Operator.GetSubject()},
		}})
	default:
		d.fail("core answered actor %T, outside the contract", actor)
	}
	return view
}

// policyFields are get_action_policy's top-level fields in the order Python's view lists them.
var policyFields = []string{"synced", "auto_approve_if", "subject", "bindings"}

func (d *viewDecoder) policyView(policy *pb.CallerActionPolicy, fields []string) jsonObject {
	view := jsonObject{}
	for _, field := range policyFields {
		if !slices.Contains(fields, field) {
			continue
		}
		var value any
		switch field {
		case "synced":
			value = policy.GetSynced()
		case "auto_approve_if":
			effective := []any{}
			for _, entry := range policy.GetAutoApproveIf() {
				effective = append(effective, jsonObject{
					{"binding", entry.GetBinding()},
					{"policy_set", entry.GetPolicySet()},
					{"source", enum(d, entry.GetSource(), "POLICY_SOURCE_")},
					{"index", int64(entry.GetIndex())},
					{"policy", d.ruleView(entry.GetPolicy())},
				})
			}
			value = effective
		case "subject":
			value = serviceAccountView(policy.GetSubject())
		case "bindings":
			bindings := []any{}
			for _, binding := range policy.GetBindings() {
				sets := []string{}
				for _, set := range binding.GetPolicySets() {
					sets = append(sets, set.GetName())
				}
				bindings = append(bindings, jsonObject{
					{"name", binding.GetName()},
					{"expires_at", d.optionalTime(binding.GetExpiresAt())},
					{"policy_sets", sets},
				})
			}
			value = bindings
		}
		view = append(view, jsonMember{field, value})
	}
	return view
}

func (d *viewDecoder) ruleView(policy *pb.Policy) jsonObject {
	switch kind := policy.GetKind().(type) {
	case *pb.Policy_ExactActions:
		return jsonObject{{"type", "exact_actions"}, {"actions", sortedLists(kind.ExactActions.GetActions())}}
	case *pb.Policy_ArgumentSchema:
		return jsonObject{
			{"type", "argument_schema"},
			{"actions", sortedLists(kind.ArgumentSchema.GetActions())},
			{"argument_schema", freeFormObject(kind.ArgumentSchema.GetArgumentSchema())},
		}
	case *pb.Policy_GithubRepository:
		return jsonObject{
			{"type", "github_repository"},
			{"actions", sortedLists(kind.GithubRepository.GetActions())},
			{"owner", kind.GithubRepository.GetOwner()},
			{"repository", kind.GithubRepository.GetRepository()},
		}
	case *pb.Policy_GithubPublicRepository:
		return jsonObject{
			{"type", "github_public_repository"},
			{"actions", sortedLists(kind.GithubPublicRepository.GetActions())},
		}
	case *pb.Policy_HomeAssistantEntityControl:
		return jsonObject{
			{"type", "home_assistant_entity_control"},
			{"actions", sortedLists(kind.HomeAssistantEntityControl.GetActions())},
			{"entities", sortedLists(kind.HomeAssistantEntityControl.GetEntities())},
		}
	default:
		d.fail("core answered policy %T, outside the contract", kind)
		return nil
	}
}

// sortedLists is a map of name lists with its keys and each list sorted, as Python's policy view sorts them.
func sortedLists(lists map[string]*pb.StringList) jsonObject {
	view := jsonObject{}
	for _, key := range slices.Sorted(maps.Keys(lists)) {
		view = append(view, jsonMember{key, slices.Sorted(slices.Values(lists[key].GetValues()))})
	}
	return view
}
