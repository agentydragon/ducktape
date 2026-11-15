package actions

import (
	"bytes"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/api/contracts/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

func indented(raw json.RawMessage) (string, error) {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return "", invocation.InvalidResponse("%v", err)
	}
	return out.String(), nil
}

func unmarshalActionJSON(raw json.RawMessage, message proto.Message) error {
	if err := protojson.Unmarshal(raw, message); err != nil {
		return invocation.InvalidResponse("%v", err)
	}
	return nil
}

func actionEnumText(value string, prefix string) string {
	return strings.ToLower(strings.TrimPrefix(value, prefix))
}

func groupLine(raw json.RawMessage) (string, error) {
	group := &action_servicepb.ActionGroup{}
	if err := unmarshalActionJSON(raw, group); err != nil {
		return "", err
	}
	status := strconv.Itoa(len(group.GetActions())) + " actions"
	if !group.GetAvailable() {
		status += " (unavailable)"
	}
	kind := actionEnumText(group.GetExecutorKind().String(), "EXECUTOR_KIND_")
	return invocation.Fields(group.GetKey(), kind, status, group.GetTitle()), nil
}

func actionLine(raw json.RawMessage) (string, error) {
	action := &action_servicepb.Action{}
	if err := unmarshalActionJSON(raw, action); err != nil {
		return "", err
	}
	return invocation.Fields(action.GetGroup()+"/"+action.GetName(), invocation.FirstLine(action.GetDescription())), nil
}

func actionDetail(raw json.RawMessage) (string, error) {
	action := &action_servicepb.Action{}
	if err := unmarshalActionJSON(raw, action); err != nil {
		return "", err
	}
	schema := []byte("{}")
	if action.GetInputSchema() != nil {
		var err error
		schema, err = protojson.Marshal(action.GetInputSchema())
		if err != nil {
			return "", invocation.InvalidResponse("input schema: %v", err)
		}
	}
	formatted, err := indented(schema)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%s/%s\n%s\ninput schema:\n%s", action.GetGroup(), action.GetName(), action.GetDescription(), formatted), nil
}

func policyText(raw json.RawMessage) (string, error) {
	policy := &action_servicepb.CallerActionPolicy{}
	if err := unmarshalActionJSON(raw, policy); err != nil {
		return "", err
	}
	lines := []string{fmt.Sprintf("subject %s/%s  synced %t", policy.GetSubject().GetNamespace(), policy.GetSubject().GetName(), policy.GetSynced())}
	for _, binding := range policy.GetBindings() {
		var sets []string
		for _, set := range binding.GetPolicySets() {
			sets = append(sets, set.GetName())
		}
		line := invocation.Fields("binding "+binding.GetName(), "sets "+strings.Join(sets, ","))
		if binding.GetExpiresAt() != nil {
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

func receiptLine(raw json.RawMessage) (string, error) {
	r, err := parseReceipt(raw)
	if err != nil {
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
		errorJSON, err := protojson.Marshal(execution.GetError())
		if err != nil {
			return "", invocation.InvalidResponse("execution error: %v", err)
		}
		line += "  error: " + string(errorJSON)
	}
	return line, nil
}

// resultText prints the text blocks of a CallToolResult, and any other result as indented JSON.
func resultText(raw json.RawMessage) (string, error) {
	var tool struct {
		Content []struct {
			Type string `json:"type"`
			Text string `json:"text"`
		} `json:"content"`
	}
	var texts []string
	// A result that is not an object has no content blocks; that is the other form, not an error.
	if json.Unmarshal(raw, &tool) == nil {
		for _, block := range tool.Content {
			if block.Type == "text" {
				texts = append(texts, block.Text)
			}
		}
	}
	if len(texts) > 0 {
		return strings.Join(texts, "\n"), nil
	}
	return indented(raw)
}

func eventLine(raw json.RawMessage) (string, error) {
	event := &action_servicepb.ActionEvent{}
	if err := unmarshalActionJSON(raw, event); err != nil {
		return "", err
	}
	line := invocation.Fields(strconv.Itoa(int(event.GetSequence())), actionEnumText(event.GetState().String(), "ACTION_STATE_"))
	if event.GetAt() != nil {
		line += "  " + event.GetAt().AsTime().Format("2006-01-02T15:04:05.999999999Z07:00")
	}
	if event.GetActor() != nil {
		actor, err := protojson.Marshal(event.GetActor())
		if err != nil {
			return "", invocation.InvalidResponse("event actor: %v", err)
		}
		line += "  actor " + string(actor)
	}
	return line, nil
}

func cancellationLine(raw json.RawMessage) (string, error) {
	cancellation := &action_servicepb.CancelActionRequestResponse{}
	if err := unmarshalActionJSON(raw, cancellation); err != nil {
		return "", err
	}
	request, err := invocation.ProtoJSON(cancellation.GetActionRequest())
	if err != nil {
		return "", err
	}
	requestLine, err := receiptLine(request)
	if err != nil {
		return "", err
	}
	outcome := actionEnumText(cancellation.GetOutcome().String(), "CANCELLATION_OUTCOME_")
	return invocation.Fields(outcome, requestLine), nil
}
