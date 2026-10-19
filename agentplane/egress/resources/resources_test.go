package resources

import (
	"fmt"
	"strings"
	"testing"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

func credential(source egressapi.CredentialSource, targets ...egressapi.Target) *egressapi.EgressCredential {
	return &egressapi.EgressCredential{
		ObjectMeta: metav1.ObjectMeta{Name: "github-pat"},
		Spec:       egressapi.EgressCredentialSpec{Description: "d", Source: source, Targets: targets},
	}
}

var (
	bearer       = egressapi.Target{Method: egressapi.SchemeToken, Header: "Authorization", Scheme: "Bearer"}
	secretSource = egressapi.CredentialSource{SecretRef: &egressapi.SecretKeyRef{Name: "pat", Key: "token"}}
	workload     = egressapi.CredentialSource{AuthenticatedWorkloadToken: &egressapi.AuthenticatedWorkloadTokenSource{}}
)

func TestCredentialFromAPISources(t *testing.T) {
	for name, tc := range map[string]struct {
		source egressapi.CredentialSource
		want   Source
	}{
		"secret":   {secretSource, SecretKey{Name: "pat", Key: "token"}},
		"workload": {workload, AuthenticatedWorkloadToken{}},
		"projected": {
			egressapi.CredentialSource{ProjectedWorkloadToken: &egressapi.ProjectedWorkloadTokenSource{Audience: "https://k8s.test"}},
			ProjectedWorkloadToken{Audience: "https://k8s.test"},
		},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := CredentialFromAPI(credential(tc.source, bearer))
			if err != nil {
				t.Fatal(err)
			}
			if got.Source != tc.want || got.Placeholder() != "agentplane-credential-github-pat" {
				t.Fatalf("got %#v", got)
			}
		})
	}
}

func TestCredentialFromAPITargets(t *testing.T) {
	for name, tc := range map[string]struct {
		target egressapi.Target
		want   Target
	}{
		"whole value":    {egressapi.Target{Method: egressapi.WholeValue, Header: "X-Api-Key"}, WholeValue{Header: "X-Api-Key"}},
		"scheme token":   {bearer, SchemeToken{Header: "Authorization", Scheme: "Bearer"}},
		"basic username": {egressapi.Target{Method: egressapi.BasicUsername, Header: "Authorization"}, BasicUsername{Header: "Authorization"}},
		"basic password": {egressapi.Target{Method: egressapi.BasicPassword, Header: "Authorization"}, BasicPassword{Header: "Authorization"}},
		"basic whole":    {egressapi.Target{Method: egressapi.BasicWhole, Header: "Authorization"}, BasicWhole{Header: "Authorization"}},
		"json field":     {egressapi.Target{Method: egressapi.JSONField, Field: "password"}, JSONField{Field: "password"}},
	} {
		t.Run(name, func(t *testing.T) {
			got, err := CredentialFromAPI(credential(workload, tc.target))
			if err != nil || got.Targets[0] != tc.want {
				t.Fatalf("got %#v, %v", got.Targets, err)
			}
			if declared := got.Targets[0].ToAPI(); declared != tc.target {
				t.Errorf("ToAPI() = %#v, want the target as declared, %#v", declared, tc.target)
			}
		})
	}
}

func TestCredentialFromAPIRefusesWhatItDoesNotUnderstand(t *testing.T) {
	noDescription := credential(workload, bearer)
	noDescription.Spec.Description = ""
	noName := credential(workload, bearer)
	noName.Name = ""
	for name, credential := range map[string]*egressapi.EgressCredential{
		"no name":                    noName,
		"no description":             noDescription,
		"no source":                  credential(egressapi.CredentialSource{}, bearer),
		"two sources":                credential(egressapi.CredentialSource{SecretRef: secretSource.SecretRef, AuthenticatedWorkloadToken: workload.AuthenticatedWorkloadToken}, bearer),
		"projected without audience": credential(egressapi.CredentialSource{ProjectedWorkloadToken: &egressapi.ProjectedWorkloadTokenSource{}}, bearer),
		"no targets":                 credential(workload),
		"scheme token without scheme": credential(workload,
			egressapi.Target{Method: egressapi.SchemeToken, Header: "Authorization"}),
		"scheme on a basic target": credential(workload,
			egressapi.Target{Method: egressapi.BasicWhole, Header: "Authorization", Scheme: "Bearer"}),
		"header on a json target": credential(workload,
			egressapi.Target{Method: egressapi.JSONField, Header: "X", Field: "password"}),
		"field on a header target": credential(workload,
			egressapi.Target{Method: egressapi.WholeValue, Header: "X", Field: "password"}),
		"json target without a field": credential(workload, egressapi.Target{Method: egressapi.JSONField}),
		"unknown method":              credential(workload, egressapi.Target{Method: "bearer", Header: "X"}),
		"no header":                   credential(workload, egressapi.Target{Method: egressapi.WholeValue}),
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := CredentialFromAPI(credential); err == nil {
				t.Fatal("converted")
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
