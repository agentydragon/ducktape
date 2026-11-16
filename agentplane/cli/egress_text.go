package cli

import (
	"fmt"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	egressrulespb "github.com/agentydragon/ducktape/agentplane/egress/api/rules/egress_rulespb"
)

func rulesText(rules *egressrulespb.GetRulesResponse) (string, error) {
	orAny := func(values *egressrulespb.StringList) string {
		if values == nil {
			return "*"
		}
		return strings.Join(values.GetValues(), ",")
	}
	var caller string
	switch {
	case rules.GetSubject() != nil:
		caller = fmt.Sprintf("subject %s/%s", rules.GetSubject().GetNamespace(), rules.GetSubject().GetName())
	case rules.GetIdentity() != nil:
		caller = "identity " + rules.GetIdentity().GetName()
	default:
		return "", invocation.InvalidResponse("rules name no caller")
	}
	lines := []string{caller}
	for _, policy := range rules.GetPolicies() {
		for _, rule := range policy.GetRules() {
			line := invocation.Fields(policy.GetName(), strings.Join(rule.GetHosts(), ","), orAny(rule.GetMethods()), orAny(rule.GetPaths()))
			if credential := rule.GetCredential(); credential != nil {
				presented := make([]string, len(credential.GetTargets()))
				for i, place := range credential.GetTargets() {
					presented[i] = credentialPresentation(place, credential.GetPlaceholder())
				}
				line += fmt.Sprintf("  credential %s: send %s", credential.GetName(), strings.Join(presented, "  or  "))
				if credential.GetDescription() != "" {
					line += "  " + credential.GetDescription()
				}
			}
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n"), nil
}

// credentialPresentation is how a request carries placeholder at this target, as the egress credentials guide
// describes each method. Unknown methods retain their wire number, so a newer service's rules still print.
func credentialPresentation(target *egressrulespb.Target, placeholder string) string {
	switch target.GetMethod() {
	case egressrulespb.Target_METHOD_SCHEME_TOKEN:
		return fmt.Sprintf("%s: %s %s", target.GetHeader(), target.GetScheme(), placeholder)
	case egressrulespb.Target_METHOD_WHOLE_VALUE:
		return fmt.Sprintf("%s: %s", target.GetHeader(), placeholder)
	case egressrulespb.Target_METHOD_BASIC_USERNAME:
		return fmt.Sprintf("%s: Basic with the username %s", target.GetHeader(), placeholder)
	case egressrulespb.Target_METHOD_BASIC_PASSWORD:
		return fmt.Sprintf("%s: Basic with the password %s", target.GetHeader(), placeholder)
	case egressrulespb.Target_METHOD_BASIC_WHOLE:
		return fmt.Sprintf("%s: Basic with the whole user:password %s", target.GetHeader(), placeholder)
	case egressrulespb.Target_METHOD_JSON_FIELD:
		return fmt.Sprintf("the top-level JSON field %q as %s", target.GetField(), placeholder)
	default:
		return fmt.Sprintf("%s %s (see --json)", target.GetMethod(), placeholder)
	}
}
