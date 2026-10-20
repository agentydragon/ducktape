package main

import (
	"fmt"
	"net/http"
	"strings"
	"testing"
)

// rulesWith is the proto-JSON of GET /v1/rules for one rule that carries a credential with these targets.
func rulesWith(targets string) string {
	return fmt.Sprintf(`{"subject":{"namespace":"test-ns","name":"test-sandbox"},"policies":[{"name":"test-policy","rules":[
		{"hosts":["api.example.test"],"methods":{"values":["GET"]},"credential":{"name":"test-credential","description":"d","placeholder":"agentplane-credential-test-credential","targets":%s}},
		{"hosts":["open.example.test"],"credential":null}]}]}`, targets)
}

func TestRulesTextShowsHowToPresentTheCredential(t *testing.T) {
	const rule = "test-policy  api.example.test  GET  *  credential test-credential: send "
	const openRule = "test-policy  open.example.test  *  *"
	cases := []struct{ name, targets, want string }{
		{
			"scheme token", `[{"method":"METHOD_SCHEME_TOKEN","header":"Authorization","scheme":"Bearer"}]`,
			"Authorization: Bearer agentplane-credential-test-credential",
		},
		{
			"whole header value", `[{"method":"METHOD_WHOLE_VALUE","header":"X-Test-Key"}]`,
			"X-Test-Key: agentplane-credential-test-credential",
		},
		{
			"basic username", `[{"method":"METHOD_BASIC_USERNAME","header":"Authorization"}]`,
			"Authorization: Basic with the username agentplane-credential-test-credential",
		},
		{
			"basic password", `[{"method":"METHOD_BASIC_PASSWORD","header":"Authorization"}]`,
			"Authorization: Basic with the password agentplane-credential-test-credential",
		},
		{
			"basic whole", `[{"method":"METHOD_BASIC_WHOLE","header":"Authorization"}]`,
			"Authorization: Basic with the whole user:password agentplane-credential-test-credential",
		},
		{
			"JSON field", `[{"method":"METHOD_JSON_FIELD","field":"token"}]`,
			`the top-level JSON field "token" as agentplane-credential-test-credential`,
		},
		{
			"several targets are alternatives",
			`[{"method":"METHOD_SCHEME_TOKEN","header":"Authorization","scheme":"Bearer"},{"method":"METHOD_WHOLE_VALUE","header":"X-Test-Key"}]`,
			"Authorization: Bearer agentplane-credential-test-credential  or  X-Test-Key: agentplane-credential-test-credential",
		},
		{
			"a method from a newer service is named", `[{"method":"METHOD_FUTURE"}]`,
			"METHOD_FUTURE agentplane-credential-test-credential (see --json)",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
				reply(w, 200, rulesWith(tc.targets))
			})
			code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules")
			want := "subject test-ns/test-sandbox\n" + rule + tc.want + "\n" + openRule
			if code != 0 || strings.TrimSpace(stdout) != want {
				t.Errorf("exit %d, stdout %q; want exit 0 and %q", code, stdout, want)
			}
		})
	}
}
