package resources

import (
	"fmt"
	"strings"
	"testing"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
)

func credentialJSON(source, targets string) []byte {
	return []byte(`{"metadata":{"name":"github-pat"},"spec":{"description":"d","source":` + source + `,"targets":` + targets + `}}`)
}

const bearer = `[{"header":"Authorization","method":"schemeToken","scheme":"Bearer"}]`

func TestParseCredentialSources(t *testing.T) {
	for name, tc := range map[string]struct {
		source string
		want   Source
	}{
		"secret":    {`{"secretRef":{"name":"pat","key":"token"}}`, SecretKey{Name: "pat", Key: "token"}},
		"workload":  {`{"authenticatedWorkloadToken":{}}`, AuthenticatedWorkloadToken{}},
		"projected": {`{"projectedWorkloadToken":{"audience":"https://k8s.test"}}`, ProjectedWorkloadToken{Audience: "https://k8s.test"}},
	} {
		t.Run(name, func(t *testing.T) {
			credential, err := ParseCredential(credentialJSON(tc.source, bearer))
			if err != nil {
				t.Fatal(err)
			}
			if credential.Source != tc.want || credential.Placeholder() != "agentplane-credential-github-pat" {
				t.Fatalf("got %#v", credential)
			}
		})
	}
}

func TestParseCredentialRefusesWhatItDoesNotUnderstand(t *testing.T) {
	for name, raw := range map[string][]byte{
		"no source":                  credentialJSON(`{}`, bearer),
		"two sources":                credentialJSON(`{"secretRef":{"name":"a","key":"b"},"authenticatedWorkloadToken":{}}`, bearer),
		"unknown source":             credentialJSON(`{"forgedSource":{}}`, bearer),
		"projected without audience": credentialJSON(`{"projectedWorkloadToken":{}}`, bearer),
		"no targets":                 credentialJSON(`{"authenticatedWorkloadToken":{}}`, `[]`),
		"scheme token without scheme": credentialJSON(`{"authenticatedWorkloadToken":{}}`,
			`[{"header":"Authorization","method":"schemeToken"}]`),
		"scheme on a basic target": credentialJSON(`{"authenticatedWorkloadToken":{}}`,
			`[{"header":"Authorization","method":"basicWhole","scheme":"Bearer"}]`),
		"header on a json target": credentialJSON(`{"authenticatedWorkloadToken":{}}`,
			`[{"header":"X","method":"jsonField","field":"password"}]`),
		"unknown method": credentialJSON(`{"authenticatedWorkloadToken":{}}`, `[{"header":"X","method":"bearer"}]`),
		"unknown target field": credentialJSON(`{"authenticatedWorkloadToken":{}}`,
			`[{"header":"X","method":"wholeValue","extra":1}]`),
		"no header": credentialJSON(`{"authenticatedWorkloadToken":{}}`, `[{"method":"wholeValue"}]`),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseCredential(raw); err == nil {
				t.Fatal("parsed")
			}
		})
	}
}

func TestParseRuleRefusesABlanketThatSubstitutesOrReachesIn(t *testing.T) {
	for _, hosts := range [][]string{{"*"}, {"api.github.com", "*"}} {
		for _, rule := range []policyapi.Rule{
			{Hosts: hosts, CredentialRef: &policyapi.CredentialRef{Name: "github-pat"}},
			{Hosts: hosts, ClusterInternal: true},
		} {
			if _, err := ParseRule(rule); err == nil {
				t.Fatalf("parsed %#v", rule)
			}
		}
	}
	if _, err := ParseRule(policyapi.Rule{}); err == nil {
		t.Fatal("a rule needs hosts")
	}
}

func TestParseRuleKeepsAbsentDistinctFromEmpty(t *testing.T) {
	rule, err := ParseRule(policyapi.Rule{Hosts: []string{"a.test"}, Methods: []string{}, Paths: nil})
	if err != nil {
		t.Fatal(err)
	}
	if rule.Methods == nil || rule.Paths != nil {
		t.Fatalf("an empty method list admits none and an absent path list admits any: %#v", rule)
	}
}

func TestPathGlob(t *testing.T) {
	for _, tc := range []struct {
		pattern, path string
		want          bool
	}{
		{"/repos/*/contents", "/repos/a/contents", true},
		{"/repos/*/contents", "/repos/a/b/contents", false},
		{"/repos/**", "/repos/a/b/c", true},
		{"/repos/**", "/repos", false},
		{"/repos/**/contents", "/repos/a/b/contents", true},
		{"/user", "/user?x=1", true},
		{"/user", "/users", false},
		{"/**", "/anything/at/all", true},
		{"/a.b", "/axb", false},
	} {
		if got := NewPathGlob(tc.pattern).Matches(tc.path); got != tc.want {
			t.Errorf("%q against %q: got %v", tc.pattern, tc.path, got)
		}
	}
}

func TestSecretValueIsNotPrinted(t *testing.T) {
	secret := NewSecret("pat", map[string]string{"token": "real-value"})
	if got := fmt.Sprintf("%v %+v %#v", secret, secret, secret); strings.Contains(got, "real-value") {
		t.Fatalf("printed the value: %s", got)
	}
}
