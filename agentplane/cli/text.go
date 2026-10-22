package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

// The text forms are for a reader; a script uses --json.

func fields(values ...string) string {
	return strings.Join(values, "  ")
}

func firstLine(text string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(text), "\n")
	return line
}

func indented(raw json.RawMessage) (string, error) {
	var out bytes.Buffer
	if err := json.Indent(&out, raw, "", "  "); err != nil {
		return "", invalidResponse("%v", err)
	}
	return out.String(), nil
}

func unmarshalActionJSON(raw json.RawMessage, message proto.Message) error {
	if err := protojson.Unmarshal(raw, message); err != nil {
		return invalidResponse("%v", err)
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
	return fields(group.GetKey(), kind, status, group.GetTitle()), nil
}

func actionLine(raw json.RawMessage) (string, error) {
	action := &action_servicepb.Action{}
	if err := unmarshalActionJSON(raw, action); err != nil {
		return "", err
	}
	return fields(action.GetGroup()+"/"+action.GetName(), firstLine(action.GetDescription())), nil
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
			return "", invalidResponse("input schema: %v", err)
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
		line := fields("binding "+binding.GetName(), "sets "+strings.Join(sets, ","))
		if binding.GetExpiresAt() != nil {
			line += "  expires " + binding.GetExpiresAt().AsTime().Format("2006-01-02T15:04:05.999999999Z07:00")
		}
		lines = append(lines, line)
	}
	for _, entry := range policy.GetAutoApproveIf() {
		var actions []string
		var policyType string
		var actionNames map[string]*action_servicepb.StringList
		switch value := entry.GetPolicy().GetKind().(type) {
		case *action_servicepb.Policy_ExactActions:
			policyType, actionNames = "exact_actions", value.ExactActions.GetActions()
		case *action_servicepb.Policy_ArgumentSchema:
			policyType, actionNames = "argument_schema", value.ArgumentSchema.GetActions()
		case *action_servicepb.Policy_GithubRepository:
			policyType, actionNames = "github_repository", value.GithubRepository.GetActions()
		case *action_servicepb.Policy_GithubPublicRepository:
			policyType, actionNames = "github_public_repository", value.GithubPublicRepository.GetActions()
		case *action_servicepb.Policy_HomeAssistantEntityControl:
			policyType, actionNames = "home_assistant_entity_control", value.HomeAssistantEntityControl.GetActions()
		default:
			return "", invalidResponse("policy entry %q has no policy kind", entry.GetPolicySet())
		}
		groups := make([]string, 0, len(actionNames))
		for group := range actionNames {
			groups = append(groups, group)
		}
		sort.Strings(groups)
		for _, group := range groups {
			for _, name := range actionNames[group].GetValues() {
				actions = append(actions, group+"/"+name)
			}
		}
		lines = append(lines, fields(fmt.Sprintf("auto-approve %s#%d", entry.GetPolicySet(), entry.GetIndex()), policyType, strings.Join(actions, ",")))
	}
	return strings.Join(lines, "\n"), nil
}

func receiptLine(raw json.RawMessage) (string, error) {
	r, err := parseReceipt(raw)
	if err != nil {
		return "", err
	}
	state := actionEnumText(r.GetState().String(), "ACTION_STATE_")
	line := fields(r.GetId(), state, r.GetAction().GetGroup()+"/"+r.GetAction().GetName(), strconv.Quote(r.GetTitle()))
	if decision := r.GetDecision(); decision != nil {
		for _, reason := range []*string{decision.DecisionNote, decision.ReasonDescription} {
			if reason != nil {
				line += "  decision: " + strconv.Quote(*reason)
				break
			}
		}
	}
	if execution := r.GetExecution(); execution != nil && execution.GetError() != nil {
		errorJSON, err := protojson.Marshal(execution.GetError())
		if err != nil {
			return "", invalidResponse("execution error: %v", err)
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
	line := fields(strconv.Itoa(int(event.GetSequence())), actionEnumText(event.GetState().String(), "ACTION_STATE_"))
	if event.GetAt() != nil {
		line += "  " + event.GetAt().AsTime().Format("2006-01-02T15:04:05.999999999Z07:00")
	}
	if event.GetActor() != nil {
		actor, err := protojson.Marshal(event.GetActor())
		if err != nil {
			return "", invalidResponse("event actor: %v", err)
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
	request, err := protoJSON(cancellation.GetActionRequest())
	if err != nil {
		return "", err
	}
	requestLine, err := receiptLine(request)
	if err != nil {
		return "", err
	}
	outcome := actionEnumText(cancellation.GetOutcome().String(), "CANCELLATION_OUTCOME_")
	return fields(outcome, requestLine), nil
}

func rulesText(raw json.RawMessage) (string, error) {
	type list struct {
		Values []string `json:"values"`
	}
	rules, err := decode[struct {
		Subject struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		} `json:"subject"`
		Policies []struct {
			Name  string `json:"name"`
			Rules []struct {
				Hosts      []string `json:"hosts"`
				Methods    *list    `json:"methods"`
				Paths      *list    `json:"paths"`
				Credential *struct {
					Name        string             `json:"name"`
					Placeholder string             `json:"placeholder"`
					Targets     []credentialTarget `json:"targets"`
				} `json:"credential"`
			} `json:"rules"`
		} `json:"policies"`
	}](raw)
	if err != nil {
		return "", err
	}
	orAny := func(values *list) string {
		if values == nil {
			return "*"
		}
		return strings.Join(values.Values, ",")
	}
	lines := []string{fmt.Sprintf("subject %s/%s", rules.Subject.Namespace, rules.Subject.Name)}
	for _, policy := range rules.Policies {
		for _, rule := range policy.Rules {
			line := fields(policy.Name, strings.Join(rule.Hosts, ","), orAny(rule.Methods), orAny(rule.Paths))
			if credential := rule.Credential; credential != nil {
				presented := make([]string, len(credential.Targets))
				for i, place := range credential.Targets {
					presented[i] = place.presenting(credential.Placeholder)
				}
				line += fmt.Sprintf("  credential %s: send %s", credential.Name, strings.Join(presented, "  or  "))
			}
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n"), nil
}

// credentialTarget is one place the proxy substitutes a credential; the method selects which fields it carries.
type credentialTarget struct {
	Method string `json:"method"`
	Header string `json:"header"`
	Field  string `json:"field"`
	Scheme string `json:"scheme"`
}

// presenting is how a request carries placeholder at this target, as the egress credentials guide
// describes each method. A method this build does not know is named, so a newer service's rules still print.
func (t credentialTarget) presenting(placeholder string) string {
	switch t.Method {
	case "METHOD_SCHEME_TOKEN":
		return fmt.Sprintf("%s: %s %s", t.Header, t.Scheme, placeholder)
	case "METHOD_WHOLE_VALUE":
		return fmt.Sprintf("%s: %s", t.Header, placeholder)
	case "METHOD_BASIC_USERNAME":
		return fmt.Sprintf("%s: Basic with the username %s", t.Header, placeholder)
	case "METHOD_BASIC_PASSWORD":
		return fmt.Sprintf("%s: Basic with the password %s", t.Header, placeholder)
	case "METHOD_BASIC_WHOLE":
		return fmt.Sprintf("%s: Basic with the whole user:password %s", t.Header, placeholder)
	case "METHOD_JSON_FIELD":
		return fmt.Sprintf("the top-level JSON field %q as %s", t.Field, placeholder)
	default:
		return fmt.Sprintf("%s %s (see --json)", t.Method, placeholder)
	}
}
