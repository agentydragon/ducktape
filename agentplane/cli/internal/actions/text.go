package actions

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"
)

func indented(message proto.Message) (string, error) {
	raw, err := invocation.ProtoJSON(message)
	if err != nil {
		return "", err
	}
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return "", invocation.InvalidResponse("%v", err)
	}
	return out.String(), nil
}

func actionEnumText(value string, prefix string) string {
	return strings.ToLower(strings.TrimPrefix(value, prefix))
}

func groupLine(group *action_servicepb.ActionGroup) (string, error) {
	status := strconv.Itoa(len(group.GetActions())) + " actions"
	warnings := 0
	for _, action := range group.GetActions() {
		if action.GetInputSchemaValidation().GetStatus() == action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
			warnings++
		}
	}
	if warnings > 0 {
		word := "warning"
		if warnings != 1 {
			word += "s"
		}
		status += fmt.Sprintf(" (%d schema %s)", warnings, word)
	}
	if !group.GetAvailable() {
		status += " (unavailable)"
	}
	kind := actionEnumText(group.GetExecutorKind().String(), "EXECUTOR_KIND_")
	return invocation.Fields(group.GetKey(), kind, status, group.GetTitle()), nil
}

func actionLine(action *action_servicepb.Action) (string, error) {
	line := invocation.Fields(action.GetGroup()+"/"+action.GetName(), invocation.FirstLine(action.GetDescription()))
	if action.GetInputSchemaValidation().GetStatus() == action_servicepb.InputSchemaValidationStatus_INPUT_SCHEMA_VALIDATION_STATUS_NOT_ENFORCED {
		line += "  [input schema not enforced]"
	}
	return line, nil
}

func actionDetail(action *action_servicepb.Action) (string, error) {
	formatted, err := indented(action.GetInputSchema())
	if err != nil {
		return "", err
	}
	validation := action.GetInputSchemaValidation()
	status := actionEnumText(validation.GetStatus().String(), "INPUT_SCHEMA_VALIDATION_STATUS_")
	if validation.GetDetail() != "" {
		status += ": " + validation.GetDetail()
	}
	return fmt.Sprintf("%s/%s\n%s\ninput schema validation: %s\ninput schema:\n%s", action.GetGroup(), action.GetName(), action.GetDescription(), status, formatted), nil
}

func policyText(policy *action_servicepb.CallerActionPolicy) (string, error) {
	lines := []string{fmt.Sprintf("subject %s/%s  synced %t", policy.GetSubject().GetNamespace(), policy.GetSubject().GetName(), policy.GetSynced())}
	for _, binding := range policy.GetBindings() {
		var sets []string
		for _, set := range binding.GetPolicySets() {
			sets = append(sets, set.GetName())
		}
		line := invocation.Fields("binding "+binding.GetName(), "sets "+strings.Join(sets, ","))
		if binding.GetExpiresAt() != nil {
			if err := binding.GetExpiresAt().CheckValid(); err != nil {
				return "", invocation.InvalidResponse("expires_at: %v", err)
			}
			line += "  expires " + binding.GetExpiresAt().AsTime().Format("2006-01-02T15:04:05.999999999Z07:00")
		}
		lines = append(lines, line)
	}
	for _, entry := range policy.GetAutoApproveIf() {
		var actions []string
		var policyType string
		var groups []*action_servicepb.GroupActions
		switch value := entry.GetPolicy().GetKind().(type) {
		case *action_servicepb.Policy_ExactActions:
			policyType, groups = "exact_actions", value.ExactActions.GetActions()
		case *action_servicepb.Policy_ArgumentSchema:
			policyType, groups = "argument_schema", value.ArgumentSchema.GetActions()
		case *action_servicepb.Policy_GithubRepository:
			policyType, groups = "github_repository", value.GithubRepository.GetActions()
		case *action_servicepb.Policy_GithubPublicRepository:
			policyType, groups = "github_public_repository", value.GithubPublicRepository.GetActions()
		case *action_servicepb.Policy_HomeAssistantEntityControl:
			policyType, groups = "home_assistant_entity_control", value.HomeAssistantEntityControl.GetActions()
		default:
			return "", invocation.InvalidResponse("policy entry %q has no policy kind", entry.GetPolicySet())
		}
		for _, group := range groups {
			for _, name := range group.GetNames() {
				actions = append(actions, group.GetGroup()+"/"+name)
			}
		}
		lines = append(lines, invocation.Fields(fmt.Sprintf("auto-approve %s#%d", entry.GetPolicySet(), entry.GetIndex()), policyType, strings.Join(actions, ",")))
	}
	return strings.Join(lines, "\n"), nil
}

func receiptLine(r *action_servicepb.ActionRequest) (string, error) {
	if err := validateActionRequest(r); err != nil {
		return "", err
	}
	state := actionEnumText(r.GetState().String(), "ACTION_STATE_")
	line := invocation.Fields(r.GetId(), state, r.GetAction().GetGroup()+"/"+r.GetAction().GetName(), strconv.Quote(r.GetTitle()))
	if decision := r.GetDecision(); decision != nil {
		var reason *string
		switch decider := decision.GetDecider().(type) {
		case *action_servicepb.Decision_Operator:
			reason = decider.Operator.DecisionNote
		case *action_servicepb.Decision_Policy:
			reason = decider.Policy.ReasonDescription
		}
		if reason != nil {
			line += "  decision: " + strconv.Quote(*reason)
		}
	}
	if execution := r.GetExecution(); execution != nil && execution.GetError() != nil {
		errorJSON, err := invocation.ProtoJSON(execution.GetError())
		if err != nil {
			return "", err
		}
		line += "  error: " + string(errorJSON)
	}
	return line, nil
}

// resultText prints the text blocks of a CallToolResult, and any other result as indented JSON.
func resultText(result *structpb.Value) (string, error) {
	var texts []string
	for _, value := range result.GetStructValue().GetFields()["content"].GetListValue().GetValues() {
		block := value.GetStructValue()
		if block == nil {
			if _, null := value.GetKind().(*structpb.Value_NullValue); null {
				continue
			}
			return indented(result)
		}
		// A malformed text block leaves the whole result visible as JSON.
		for _, key := range []string{"type", "text"} {
			switch block.GetFields()[key].GetKind().(type) {
			case nil, *structpb.Value_NullValue, *structpb.Value_StringValue:
			default:
				return indented(result)
			}
		}
		if block.GetFields()["type"].GetStringValue() == "text" {
			texts = append(texts, block.GetFields()["text"].GetStringValue())
		}
	}
	if len(texts) > 0 {
		return strings.Join(texts, "\n"), nil
	}
	return indented(result)
}

func eventLine(event *action_servicepb.ActionEvent) (string, error) {
	line := invocation.Fields(strconv.Itoa(int(event.GetSequence())), actionEnumText(event.GetState().String(), "ACTION_STATE_"))
	if event.GetAt() != nil {
		if err := event.GetAt().CheckValid(); err != nil {
			return "", invocation.InvalidResponse("at: %v", err)
		}
		line += "  " + event.GetAt().AsTime().Format("2006-01-02T15:04:05.999999999Z07:00")
	}
	if event.GetActor() != nil {
		actor, err := invocation.ProtoJSON(event.GetActor())
		if err != nil {
			return "", err
		}
		line += "  actor " + string(actor)
	}
	return line, nil
}

func cancellationLine(cancellation *action_servicepb.CancelActionRequestResponse) (string, error) {
	requestLine, err := receiptLine(cancellation.GetActionRequest())
	if err != nil {
		return "", err
	}
	outcome := actionEnumText(cancellation.GetOutcome().String(), "CANCELLATION_OUTCOME_")
	return invocation.Fields(outcome, requestLine), nil
}
