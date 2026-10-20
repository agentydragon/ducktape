package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strconv"
	"strings"
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

func groupLine(raw json.RawMessage) (string, error) {
	group, err := decode[struct {
		Key          string            `json:"key"`
		Title        string            `json:"title"`
		ExecutorKind string            `json:"executor_kind"`
		Available    bool              `json:"available"`
		Actions      []json.RawMessage `json:"actions"`
	}](raw)
	if err != nil {
		return "", err
	}
	status := strconv.Itoa(len(group.Actions)) + " actions"
	if !group.Available {
		status += " (unavailable)"
	}
	return fields(group.Key, group.ExecutorKind, status, group.Title), nil
}

func actionLine(raw json.RawMessage) (string, error) {
	action, err := decode[struct {
		Group       string `json:"group"`
		Name        string `json:"name"`
		Description string `json:"description"`
	}](raw)
	return fields(action.Group+"/"+action.Name, firstLine(action.Description)), err
}

func actionDetail(raw json.RawMessage) (string, error) {
	action, err := decode[struct {
		Group       string          `json:"group"`
		Name        string          `json:"name"`
		Description string          `json:"description"`
		InputSchema json.RawMessage `json:"input_schema"`
	}](raw)
	if err != nil {
		return "", err
	}
	schema, err := indented(action.InputSchema)
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%s/%s\n%s\ninput schema:\n%s", action.Group, action.Name, action.Description, schema), nil
}

func policyText(raw json.RawMessage) (string, error) {
	policy, err := decode[struct {
		Subject struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		} `json:"subject"`
		Synced   bool `json:"synced"`
		Bindings []struct {
			Name       string   `json:"name"`
			ExpiresAt  *string  `json:"expires_at"`
			PolicySets []string `json:"policy_sets"`
		} `json:"bindings"`
		AutoApproveIf []struct {
			PolicySet string `json:"policy_set"`
			Index     int    `json:"index"`
			Policy    struct {
				Type    string              `json:"type"`
				Actions map[string][]string `json:"actions"`
			} `json:"policy"`
		} `json:"auto_approve_if"`
	}](raw)
	if err != nil {
		return "", err
	}
	lines := []string{fmt.Sprintf("subject %s/%s  synced %t", policy.Subject.Namespace, policy.Subject.Name, policy.Synced)}
	for _, binding := range policy.Bindings {
		line := fields("binding "+binding.Name, "sets "+strings.Join(binding.PolicySets, ","))
		if binding.ExpiresAt != nil {
			line += "  expires " + *binding.ExpiresAt
		}
		lines = append(lines, line)
	}
	for _, entry := range policy.AutoApproveIf {
		var actions []string
		for group, names := range entry.Policy.Actions {
			for _, name := range names {
				actions = append(actions, group+"/"+name)
			}
		}
		lines = append(lines, fields(fmt.Sprintf("auto-approve %s#%d", entry.PolicySet, entry.Index), entry.Policy.Type, strings.Join(actions, ",")))
	}
	return strings.Join(lines, "\n"), nil
}

func receiptLine(raw json.RawMessage) (string, error) {
	r, err := parseReceipt(raw)
	if err != nil {
		return "", err
	}
	line := fields(r.ID, r.State, r.Action.Group+"/"+r.Action.Name, strconv.Quote(r.Title))
	if r.Decision != nil {
		for _, reason := range []*string{r.Decision.Note, r.Decision.Reason} {
			if reason != nil {
				line += "  decision: " + strconv.Quote(*reason)
				break
			}
		}
	}
	if r.Execution != nil && len(r.Execution.Error) > 0 && string(r.Execution.Error) != "null" {
		line += "  error: " + string(r.Execution.Error)
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
	event, err := decode[struct {
		Sequence int             `json:"sequence"`
		State    string          `json:"state"`
		At       string          `json:"at"`
		Actor    json.RawMessage `json:"actor"`
	}](raw)
	line := fields(strconv.Itoa(event.Sequence), event.State, event.At)
	if len(event.Actor) > 0 && string(event.Actor) != "null" {
		line += "  actor " + string(event.Actor)
	}
	return line, err
}

func cancellationLine(raw json.RawMessage) (string, error) {
	cancellation, err := decode[struct {
		Outcome string          `json:"outcome"`
		Request json.RawMessage `json:"request"`
	}](raw)
	if err != nil {
		return "", err
	}
	request, err := receiptLine(cancellation.Request)
	return fields(cancellation.Outcome, request), err
}

func subscriptionLine(raw json.RawMessage) (string, error) {
	subscription, err := decode[struct {
		ID        string          `json:"id"`
		Source    json.RawMessage `json:"source"`
		Version   int             `json:"version"`
		Cancelled bool            `json:"cancelled"`
		ExpiresAt string          `json:"expires_at"`
		ErrorKind *string         `json:"error_kind"`
		Error     *string         `json:"error"`
	}](raw)
	if err != nil {
		return "", err
	}
	source, err := decode[struct {
		Provider   string `json:"provider"`
		Repository string `json:"repository"`
		RequestID  string `json:"request_id"`
		Subject    struct {
			Kind   string `json:"kind"`
			Number int    `json:"number"`
			Name   string `json:"name"`
			SHA    string `json:"sha"`
		} `json:"subject"`
	}](subscription.Source)
	if err != nil {
		return "", err
	}
	label := source.Provider + " " + source.RequestID
	if source.Provider == "github" {
		subject := source.Subject.Name + source.Subject.SHA
		if source.Subject.Number > 0 {
			subject = strconv.Itoa(source.Subject.Number)
		}
		label = fmt.Sprintf("github %s %s:%s", source.Repository, source.Subject.Kind, subject)
	}
	line := fields(subscription.ID, label, "v"+strconv.Itoa(subscription.Version), "expires "+subscription.ExpiresAt)
	if subscription.Cancelled {
		line += "  cancelled"
	}
	if subscription.ErrorKind != nil {
		line += "  error " + *subscription.ErrorKind
	}
	return line, nil
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

func sourcesText(raw json.RawMessage) (string, error) {
	sources, err := decode[map[string]struct {
		Content string `json:"content"`
	}](raw)
	if err != nil {
		return "", err
	}
	lines := []string{}
	for _, provider := range slices.Sorted(maps.Keys(sources)) {
		lines = append(lines, fields(provider, sources[provider].Content))
	}
	return strings.Join(lines, "\n"), nil
}
