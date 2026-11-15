package cli

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
)

func rulesText(raw json.RawMessage) (string, error) {
	type list struct {
		Values []string `json:"values"`
	}
	rules, err := invocation.Decode[struct {
		Subject *struct {
			Namespace string `json:"namespace"`
			Name      string `json:"name"`
		} `json:"subject"`
		Identity *struct {
			Name string `json:"name"`
		} `json:"identity"`
		Policies []struct {
			Name  string `json:"name"`
			Rules []struct {
				Hosts      []string `json:"hosts"`
				Methods    *list    `json:"methods"`
				Paths      *list    `json:"paths"`
				Credential *struct {
					Name        string             `json:"name"`
					Description string             `json:"description"`
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
	var caller string
	switch {
	case rules.Subject != nil:
		caller = fmt.Sprintf("subject %s/%s", rules.Subject.Namespace, rules.Subject.Name)
	case rules.Identity != nil:
		caller = "identity " + rules.Identity.Name
	default:
		return "", invocation.InvalidResponse("rules name no caller")
	}
	lines := []string{caller}
	for _, policy := range rules.Policies {
		for _, rule := range policy.Rules {
			line := invocation.Fields(policy.Name, strings.Join(rule.Hosts, ","), orAny(rule.Methods), orAny(rule.Paths))
			if credential := rule.Credential; credential != nil {
				presented := make([]string, len(credential.Targets))
				for i, place := range credential.Targets {
					presented[i] = place.presenting(credential.Placeholder)
				}
				line += fmt.Sprintf("  credential %s: send %s", credential.Name, strings.Join(presented, "  or  "))
				if credential.Description != "" {
					line += "  " + credential.Description
				}
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
